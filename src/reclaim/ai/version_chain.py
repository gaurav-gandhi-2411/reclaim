from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from reclaim.ai.models import AICluster, AIClusterMember, AITrack

# Feature 1b's version-chain ordering (spec §2: "filename patterns (v1/v2/final/(1)/copy) +
# content similarity + mtime -> order the chain, recommend keeping the latest, surface the
# older ones for review"). This module ORDERS a set of paths already known/suspected to be one
# chain (clustering "which files belong together" is document_similarity.py's job, via the
# same MinHash/embedding pipeline at a looser threshold); this module only answers "given
# these N files, what order are they in."
#
# The filename-pattern scorer is a heuristic, not a parser with formal guarantees — real
# filenames are messy and inconsistent. It's measured against a constructed fixture set (exact-
# order accuracy + Kendall's tau, spec §7.2) rather than asserted correct by construction.
#
# SAFETY PROPERTY (ADR-0017's version-chain follow-up): version-chain is the one Feature 1b
# path that can suggest deleting a file the user still considers current, not just an
# accumulated near-dup — filename rank and mtime are two INDEPENDENT signals about which file
# is "latest," and when they disagree (see `version_signals_agree`), that disagreement is
# itself evidence the ordering isn't trustworthy enough to act on. A deletion suggestion may
# only fire when both signals agree; a disagreement always downgrades to browse-only.

_V_NUMBER_RE = re.compile(r"(?:^|[_\-\s])v(?:ersion)?[_\-\s]?(\d+)", re.IGNORECASE)
_PAREN_NUMBER_RE = re.compile(r"\((\d+)\)")
# NOT `\bfinal\b` / `\bcopy\b`: `\w` (what `\b` boundaries on) includes underscore, so a plain
# word-boundary regex fails to match "final" inside "report_final.docx" — underscore- and
# hyphen-separated filenames (the spec's own example: "final_v2_FINAL.docx") are the common
# case, not the exception, so the boundary must treat `_`/`-`/space as real separators too.
_FINAL_RE = re.compile(r"(?:^|[_\-\s.])final(?:$|[_\-\s.])", re.IGNORECASE)
_COPY_RE = re.compile(r"(?:^|[_\-\s.])copy(?:$|[_\-\s.])", re.IGNORECASE)
_FINAL_RANK_BUMP = 1000.0  # "final" outranks any bare numbered version found in practice —
# real version numbers in filenames are essentially never in the thousands.


def filename_version_rank(path: Path) -> float | None:
    """A comparable rank (higher = later in the chain), derived purely from filename
    patterns — `None` if no recognizable pattern is present (caller falls back to mtime).
    Patterns recognized: `v1`/`v2`/`version 3`, Windows auto-duplicate suffixes `(1)`/`(2)`,
    a bare `copy` marker, and `final` (which outranks any numbered version by a large fixed
    bump, since a real filename's version number is essentially never in the thousands —
    "report_v3_final" ranks above "report_v9" without a numbered final ever needing to exist).
    """
    stem = path.stem
    rank: float | None = None

    v_match = _V_NUMBER_RE.search(stem)
    if v_match:
        rank = float(v_match.group(1))

    paren_match = _PAREN_NUMBER_RE.search(stem)
    if paren_match:
        paren_rank = float(paren_match.group(1))
        rank = paren_rank if rank is None else max(rank, paren_rank)

    if rank is None and _COPY_RE.search(stem):
        rank = 1.0

    if _FINAL_RE.search(stem):
        rank = (rank or 0.0) + _FINAL_RANK_BUMP

    return rank


def order_version_chain(paths: Sequence[Path]) -> list[Path]:
    """Orders `paths` oldest-first. Primary signal: `filename_version_rank`. Files with no
    recognizable filename pattern sort as if rank 0 (i.e., treated as the earliest/base file —
    a reasonable default since an unversioned base file is usually what later copies/versions
    were derived from), with mtime breaking ties within the same rank."""

    def sort_key(path: Path) -> tuple[float, float]:
        rank = filename_version_rank(path)
        return (rank if rank is not None else -1.0, path.stat().st_mtime)

    return sorted(paths, key=sort_key)


def version_signals_agree(paths: Sequence[Path]) -> bool:
    """Safety check (GG's explicit instruction: version-chain is the one 1b path that can
    delete a genuinely-wanted file, so a deletion suggestion may only fire when the filename-
    version and modification-time signals AGREE). Compares every pair of files that BOTH have
    a recognizable `filename_version_rank` — files with no filename pattern carry no filename
    signal to conflict with mtime in the first place, so they're excluded from this check, not
    treated as a disagreement. Returns `False` the moment any ranked pair's filename-implied
    order contradicts its mtime-implied order — e.g. `report_final.docx` (rank: final, very
    high) with an EARLIER mtime than `report_v2.docx` (rank: 2, lower) is exactly the case
    GG named: the filename claims "final" is later, but the file was modified before v2 was —
    a real, ambiguous signal that must not be resolved with false confidence.
    """
    ranked = [(path, rank) for path in paths if (rank := filename_version_rank(path)) is not None]
    for i in range(len(ranked)):
        for j in range(i + 1, len(ranked)):
            path_a, rank_a = ranked[i]
            path_b, rank_b = ranked[j]
            if rank_a == rank_b:
                continue  # a tie in filename rank isn't a disagreement to flag
            mtime_a, mtime_b = path_a.stat().st_mtime, path_b.stat().st_mtime
            if mtime_a == mtime_b:
                continue  # a tie in mtime isn't a disagreement either
            filename_says_a_is_earlier = rank_a < rank_b
            mtime_says_a_is_earlier = mtime_a < mtime_b
            if filename_says_a_is_earlier != mtime_says_a_is_earlier:
                return False
    return True


def build_version_chain_cluster(
    chain_id: str, paths: Sequence[Path], *, min_content_similarity: float
) -> AICluster:
    """Wraps `order_version_chain`'s output into an `AICluster`. When `version_signals_agree`
    is True, the latest (last-ordered) file is the recommended keep, exactly as before. When
    the signals DISAGREE, no member is marked `is_recommended_keep` — per `AICluster.
    suggests_deletion`'s existing logic, that makes this cluster browse-only (surfaced for
    manual review, per GG's explicit instruction) instead of a deletion suggestion, even
    though the track itself remains deletion-eligible. Every member still carries its chain
    `position` (0-indexed, oldest first) either way — ordering information is useful for
    review regardless of whether a keeper is confidently recommended.
    `min_content_similarity` is the caller-supplied evidence that these paths genuinely belong
    to one chain (from document_similarity.py's clustering) — recorded as `raw_score`, never
    re-derived here (this function only orders, it doesn't re-verify membership)."""
    ordered = order_version_chain(paths)
    signals_agree = version_signals_agree(paths)
    members = tuple(
        AIClusterMember(
            path=path,
            size_bytes=path.stat().st_size,
            is_recommended_keep=(signals_agree and index == len(ordered) - 1),
            position=index,
        )
        for index, path in enumerate(ordered)
    )
    if signals_agree:
        rationale = (
            f"{len(members)} files ordered as a version chain by filename pattern + mtime — "
            "recommending the latest as the keeper, surfacing older versions for review "
            "(never auto-deleted)."
        )
    else:
        rationale = (
            f"{len(members)} files show filename-version and modification-time signals that "
            "DISAGREE on ordering — no deletion suggestion made; surfaced for manual review "
            "only, since confidently picking a keeper from conflicting signals risks deleting "
            "the file the user actually considers current."
        )
    return AICluster(
        cluster_id=chain_id,
        track=AITrack.VERSION_CHAIN,
        members=members,
        raw_score=min_content_similarity,
        score_kind="min_pairwise_content_similarity_within_chain",
        rationale=rationale,
    )
