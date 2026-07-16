from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# Windows FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS — set by cloud-sync filter drivers
# (OneDrive/Dropbox/Google Drive) on files that are placeholders only, not synced locally.
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

# Windows FILE_ATTRIBUTE_REPARSE_POINT — junctions/symlinks. The scanner gates recursion on
# this bit alone (never on DirEntry.is_dir()), since junctions carry FILE_ATTRIBUTE_DIRECTORY
# alongside this bit and some Python/Windows combinations still report them as traversable.
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


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
    # Stage 2 additions (scanner-populated; default to 0 so Stage 1 call sites that predate
    # the scanner — SafetyValidator tests, the golden-tree fixture builder — keep working
    # unchanged, since none of them need real dev/ino/mtime/ctime values).
    mtime: float = 0.0
    ctime: float = 0.0
    dev: int = 0
    ino: int = 0

    @property
    def is_cloud_placeholder(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)

    @property
    def is_reparse_point(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_REPARSE_POINT)


@dataclass(frozen=True, slots=True)
class SafetyResult:
    """A validator verdict plus the rationale the UI will eventually show the user."""

    record: FileRecord
    verdict: Verdict
    reason_code: str
    rationale: str


class Tier(StrEnum):
    """Final disposition tier for a candidate that has passed `SafetyValidator`.

    Spec: "No Tier for silent permanent deletion. It does not exist in v1" — the corollary
    (Stage 3) is that nothing non-blocked is ever silently dropped either: every candidate
    that isn't BLOCKED lands in exactly one of these two tiers.
    """

    A = "A"  # auto-quarantine eligible
    B = "B"  # review queue


@dataclass(frozen=True, slots=True)
class RawCandidate:
    """One rule detector's raw proposal, before `SafetyValidator` has evaluated it.

    `category` is the fine-grained id surfaced in rationale/UI text (e.g.
    "dev_artifact_node_modules"); `category_group` is the coarser id used to look up the
    matching `config.categories.*` enable flag (e.g. "dev_artifacts") — kept distinct because
    several fine-grained categories (node_modules, .venv, target/, ...) share one config
    toggle.
    """

    path: Path
    is_dir: bool
    category: str
    category_group: str
    suggested_tier: Tier
    rationale: str
    rebuild_instruction: str | None = None


@dataclass(frozen=True, slots=True)
class DuplicateCluster:
    """A group of byte-identical files (same size + full BLAKE3 hash) found by the Stage 4
    exact-duplicate pipeline. `keep` is the member chosen by the keep-heuristic and is never
    itself proposed as a candidate; every other member in `duplicates` becomes one.

    Kept as its own value object (not folded into `Candidate`) so a later stage can attach a
    per-cluster keep override without re-running clustering: `members` preserves the full
    original group, so an override just needs to pick a different element from it.
    """

    full_hash: str
    size_bytes: int
    keep: FileRecord
    duplicates: tuple[FileRecord, ...]

    @property
    def members(self) -> tuple[FileRecord, ...]:
        return (self.keep, *self.duplicates)


@dataclass(frozen=True, slots=True)
class HashSkip:
    """One file the Stage 4 dedup pipeline could not hash — read timed out (hang guard) or
    raised `OSError` (locked file, permission denied, vanished mid-scan). Recorded rather than
    silently dropped or allowed to wedge the whole pipeline: the file is simply excluded from
    duplicate-cluster consideration, and the skip is surfaced to the caller so a report can show
    it under "skipped/unreadable" instead of the run just looking incomplete."""

    path: Path
    stage: str  # "partial" | "full"
    reason: str  # "timeout" or str(OSError)


@dataclass(frozen=True, slots=True)
class MaterialityExclusionStats:
    """How many duplicate-size buckets were excluded by the materiality gate
    (`config.categories.duplicates.min_reclaim_bytes`) before any hashing ran, and their
    summed *theoretical* best-case reclaim — every member of every excluded bucket turning out
    to be an exact duplicate. Labeled "theoretical" deliberately: these buckets were never
    hashed, so this is an upper bound, never a claim about real, measured reclaim (spec: no
    fabricated confidence). Surfaced to the report so the exclusion is visible, not silent."""

    excluded_bucket_count: int
    theoretical_bytes: int


@dataclass(frozen=True, slots=True)
class Candidate:
    """A `RawCandidate` that has passed through `SafetyValidator.evaluate()` and been assigned
    a final tier — the only shape Stage 4+ (dedup pipeline, executor, UI) should ever consume.
    """

    path: Path
    is_dir: bool
    category: str
    category_group: str
    size_bytes: int
    tier: Tier
    rationale: str
    rebuild_instruction: str | None
    safety_verdict: Verdict
    safety_reason_code: str
    # ADR-0001: resolved from `config.categories.<group>.retention_days` at the same point
    # `_category_enabled`/`Tier.A` gating already happens. `None` -> `apply_batch` permanently
    # deletes this item on apply (no vault); an int -> vault + manifest + restore as before,
    # `purge`-eligible once that many days have passed.
    retention_days: int | None
