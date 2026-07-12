from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# Windows FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS — set by cloud-sync filter drivers
# (OneDrive/Dropbox/Google Drive) on files that are placeholders only, not synced locally.
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000


class Verdict(StrEnum):
    """Outcome of a SafetyValidator evaluation. Only ELIGIBLE files may ever reach Tier A."""

    BLOCKED = "blocked"
    REVIEW_ONLY = "review_only"
    ELIGIBLE = "eligible"


@dataclass(frozen=True, slots=True)
class FileRecord:
    """What the future scanner will produce for a single filesystem entry.

    Frozen dataclass (not pydantic) so SafetyValidator stays allocation-cheap and
    I/O-free — the spec's ≥100K files/min scan budget rules out per-record validation
    overhead on the hot path.
    """

    path: Path
    is_dir: bool
    size_bytes: int
    attributes: int
    ext: str
    git_repo_root: Path | None
    git_repo_clean: bool

    @property
    def is_cloud_placeholder(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)


@dataclass(frozen=True, slots=True)
class SafetyResult:
    """A validator verdict plus the rationale the UI will eventually show the user."""

    record: FileRecord
    verdict: Verdict
    reason_code: str
    rationale: str
