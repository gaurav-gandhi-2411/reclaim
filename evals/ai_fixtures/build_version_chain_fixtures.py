from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Constructed, deterministic fixture set for Feature 1b's version-chain ordering eval (spec
# §7.2: exact-order accuracy + Kendall's tau). There is no public dataset for "which order did
# GG rename his own files in" — this is a Reclaim-specific filename-convention problem, not a
# generic NLP task a public corpus would label. Per spec §7.1's own framing ("CI fixtures
# (checked in, synthetic, deterministic)"), a disclosed, constructed fixture is the right and
# honest source here, not a gap to apologize for — see ADR-0017.
#
# Filename patterns are drawn from real-world conventions this codebase's own version_chain.py
# module is built to recognize (vN/version N/Windows (N) duplicate suffix/copy/final), plus
# deliberately unversioned filenames that only mtime can order — testing BOTH signal sources
# order_version_chain.py actually uses, not just the easy case.

_SEED_MTIME = 1_700_000_000.0  # 2023-11-14 — arbitrary fixed epoch, not "now" (house rule 40's
# "no Date.now()"-equivalent for filesystem timestamps: deterministic, reproducible fixtures)


@dataclass(frozen=True, slots=True)
class VersionChainFixture:
    chain_id: str
    filenames_in_creation_order: tuple[str, ...]  # ground truth: oldest first
    # mtimes are assigned in creation order automatically; only some chains use recognizable
    # filename patterns, forcing order_version_chain to fall back to mtime for the rest.


_CHAINS: tuple[VersionChainFixture, ...] = (
    VersionChainFixture(
        "numbered_v",
        ("proposal_v1.docx", "proposal_v2.docx", "proposal_v3.docx", "proposal_final.docx"),
    ),
    VersionChainFixture(
        "windows_duplicate_suffix",
        ("budget.xlsx", "budget (1).xlsx", "budget (2).xlsx"),
    ),
    VersionChainFixture(
        "copy_marker",
        ("notes.txt", "notes - Copy.txt"),
    ),
    VersionChainFixture(
        "mixed_final_beats_high_number",
        ("report_v2.docx", "report_v5.docx", "report_FINAL.docx"),
    ),
    VersionChainFixture(
        "no_pattern_mtime_only",
        ("draft.txt", "revised.txt", "submission.txt"),
    ),
    VersionChainFixture(
        "version_word_spelled_out",
        ("summary version 1.docx", "summary version 2.docx", "summary version 3.docx"),
    ),
    VersionChainFixture(
        "underscore_final_real_world_example",
        (
            "quarterly_report.docx",
            "quarterly_report_v2.docx",
            "quarterly_report_final_v2_FINAL.docx",
        ),
    ),
    VersionChainFixture(
        "long_numbered_chain",
        tuple(f"design_v{n}.docx" for n in range(1, 7)),
    ),
)


def materialize_version_chain_fixtures(root: Path) -> list[tuple[str, list[Path]]]:
    """Writes each fixture chain's files to `root`, each file's mtime set to reflect its
    position in `filenames_in_creation_order` (earlier files get earlier mtimes, 60 seconds
    apart — comfortably distinguishable, no risk of filesystem mtime-resolution collisions).
    Returns `(chain_id, paths_in_SCRAMBLED_order)` per chain — scrambled so a test calling
    `order_version_chain` on them is exercising real re-ordering, not just confirming an
    already-sorted list."""
    root.mkdir(parents=True, exist_ok=True)
    result: list[tuple[str, list[Path]]] = []
    for fixture in _CHAINS:
        chain_dir = root / fixture.chain_id
        chain_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for index, filename in enumerate(fixture.filenames_in_creation_order):
            path = chain_dir / filename
            path.write_text(
                f"Content for {filename} in chain {fixture.chain_id}.", encoding="utf-8"
            )
            mtime = _SEED_MTIME + index * 60.0
            os.utime(path, (mtime, mtime))
            paths.append(path)
        # Deterministic "scramble": reverse the list — guarantees every chain is presented
        # out of order (a no-op shuffle would risk a chain that's already sorted by
        # coincidence, which wouldn't actually test re-ordering).
        result.append((fixture.chain_id, list(reversed(paths))))
    return result


def true_order_for(chain_id: str, root: Path) -> list[Path]:
    fixture = next(f for f in _CHAINS if f.chain_id == chain_id)
    return [root / chain_id / filename for filename in fixture.filenames_in_creation_order]


# --- Conflict fixtures: filename-version rank vs. mtime DISAGREE (ADR-0017's version-chain
# safety follow-up: GG's explicit instruction that a version-chain deletion suggestion may
# only fire when both signals agree). Unlike `_CHAINS` above (mtimes assigned strictly in
# filename-rank order, by construction), these fixtures encode explicit mtime OFFSETS per
# file, independent of filename rank, specifically to construct real disagreements.


@dataclass(frozen=True, slots=True)
class VersionChainConflictFixture:
    chain_id: str
    filenames_with_mtime_offset_seconds: tuple[tuple[str, float], ...]
    has_conflict: bool  # ground truth: does ANY pair of ranked files disagree filename vs. mtime?


_CONFLICT_CHAINS: tuple[VersionChainConflictFixture, ...] = (
    VersionChainConflictFixture(
        "final_older_than_v2",
        # The exact scenario GG named: report_final.docx (highest filename rank) modified
        # BEFORE report_v2.docx (lower filename rank).
        (("report_final.docx", 0.0), ("report_v2.docx", 100.0)),
        has_conflict=True,
    ),
    VersionChainConflictFixture(
        "v3_older_than_v1",
        (("draft_v3.docx", 0.0), ("draft_v1.docx", 100.0)),
        has_conflict=True,
    ),
    VersionChainConflictFixture(
        "one_conflicting_pair_among_three",
        # v1 and v3 individually agree with a "normal" timeline, but v2 (rank 2, should be
        # BETWEEN v1 and v3 in time) was modified AFTER v3 (rank 3) -- a single conflicting
        # pair (v2, v3) buried in an otherwise-plausible-looking chain, testing that ANY
        # pairwise disagreement anywhere blocks the deletion suggestion, not just an
        # all-or-nothing full reversal.
        (("plan_v1.docx", 0.0), ("plan_v3.docx", 60.0), ("plan_v2.docx", 200.0)),
        has_conflict=True,
    ),
    VersionChainConflictFixture(
        "genuinely_agreeing_control",
        # A control case in the SAME fixture set, structured identically (explicit offsets,
        # not the strictly-sequential _CHAINS construction) but with NO conflict — proves the
        # conflict detector doesn't cry wolf on a normal chain just because it uses the
        # offset-based construction.
        (("memo_v1.docx", 0.0), ("memo_v2.docx", 60.0), ("memo_final.docx", 120.0)),
        has_conflict=False,
    ),
)


def materialize_version_chain_conflict_fixtures(root: Path) -> list[tuple[str, list[Path], bool]]:
    """Returns `(chain_id, paths, has_conflict)` per fixture — `has_conflict` is the ground
    truth this eval checks `version_signals_agree` against."""
    root.mkdir(parents=True, exist_ok=True)
    result: list[tuple[str, list[Path], bool]] = []
    for fixture in _CONFLICT_CHAINS:
        chain_dir = root / fixture.chain_id
        chain_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for filename, offset in fixture.filenames_with_mtime_offset_seconds:
            path = chain_dir / filename
            path.write_text(
                f"Content for {filename} in chain {fixture.chain_id}.", encoding="utf-8"
            )
            mtime = _SEED_MTIME + offset
            os.utime(path, (mtime, mtime))
            paths.append(path)
        result.append((fixture.chain_id, paths, fixture.has_conflict))
    return result
