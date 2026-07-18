from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from reclaim.ai.models import AICluster, AIClusterMember, AITrack

# Feature 1b's version-chain ordering (spec §2: "filename patterns (v1/v2/final/(1)/copy) +
# content similarity + mtime -> order the chain, recommend keeping the latest, surface the
# older ones for review"). This module ORDERS a set of paths already known/suspected to be one
# chain (clustering "which files belong together" is document_similarity.py's job, via the
# same MinHash/embedding pipeline at a looser threshold — see version_chain_orchestration
# below); this module only answers "given these N files, what order are they in."
#
# The filename-pattern scorer is a heuristic, not a parser with formal guarantees — real
# filenames are messy and inconsistent. It's measured against a constructed fixture set (exact-
# order accuracy + Kendall's tau, spec §7.2) rather than asserted correct by construction.

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


def build_version_chain_cluster(
    chain_id: str, paths: Sequence[Path], *, min_content_similarity: float
) -> AICluster:
    """Wraps `order_version_chain`'s output into an `AICluster` — the latest (last-ordered)
    file is the recommended keep; every member carries its chain `position` (0-indexed,
    oldest first). `min_content_similarity` is the caller-supplied evidence that these paths
    genuinely belong to one chain (from document_similarity.py's clustering) — recorded as
    `raw_score`, never re-derived here (this function only orders, it doesn't re-verify
    membership)."""
    ordered = order_version_chain(paths)
    members = tuple(
        AIClusterMember(
            path=path,
            size_bytes=path.stat().st_size,
            is_recommended_keep=(index == len(ordered) - 1),
            position=index,
        )
        for index, path in enumerate(ordered)
    )
    return AICluster(
        cluster_id=chain_id,
        track=AITrack.VERSION_CHAIN,
        members=members,
        raw_score=min_content_similarity,
        score_kind="min_pairwise_content_similarity_within_chain",
        rationale=(
            f"{len(members)} files ordered as a version chain by filename pattern + mtime — "
            "recommending the latest as the keeper, surfacing older versions for review "
            "(never auto-deleted)."
        ),
    )
