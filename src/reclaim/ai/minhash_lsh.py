from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from reclaim.ai._optional import require

# Feature 1b Stage 1 (spec §2): the MinHash/LSH prefilter over text shingles — the cheap,
# deterministic-enough "workhorse" stage that resolves most near-dup document pairs without
# ever needing a model. Mirrors phash.py's role in Feature 1a Track A exactly: a hash-based
# prefilter callers run BEFORE the expensive stage (sentence embeddings, text_embeddings.py),
# never after (spec §0.4's two-stage compute rule — never embed a document a hash could have
# excluded).

_SHINGLE_SIZE = 5  # word n-gram size — long enough to be distinctive, short enough to survive
# a few edited/inserted words without every shingle changing.
_NUM_PERMUTATIONS = 128  # datasketch's own recommended default range for stable estimates.
_WORD_RE = re.compile(r"\w+")


def _shingles(text: str, *, k: int = _SHINGLE_SIZE) -> set[str]:
    words = _WORD_RE.findall(text.lower())
    if not words:
        return set()
    if len(words) < k:
        return {" ".join(words)}  # short text: the whole thing is one shingle
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


@dataclass(frozen=True, slots=True)
class DocumentMinHash:
    """One document's MinHash signature — `minhash_values` stored as a plain tuple of ints
    (not a `datasketch.MinHash` object) so records are trivially serializable into a future
    embedding/hash cache without a custom codec, same reasoning as `phash.ImageHashRecord`
    storing hex strings instead of `imagehash.ImageHash` objects."""

    path: Path
    shingle_count: int
    minhash_values: tuple[int, ...]


def compute_document_minhash(
    path: Path, text: str, *, num_perm: int = _NUM_PERMUTATIONS
) -> DocumentMinHash | None:
    """Returns `None` (not an error) for text that shingles to nothing (empty/whitespace-only
    extraction) — a common, expected case for a scanned-image PDF with no OCR layer, never
    worth raising for."""
    datasketch = require("datasketch", feature="MinHash near-duplicate document prefilter")
    shingle_set = _shingles(text)
    if not shingle_set:
        return None

    minhash = datasketch.MinHash(num_perm=num_perm)
    for shingle in shingle_set:
        minhash.update(shingle.encode("utf-8"))
    return DocumentMinHash(
        path=path,
        shingle_count=len(shingle_set),
        minhash_values=tuple(int(v) for v in minhash.hashvalues),
    )


def jaccard_similarity(record_a: DocumentMinHash, record_b: DocumentMinHash) -> float:
    """Estimated Jaccard similarity (0.0-1.0, higher = more similar) between two documents'
    shingle sets, via their MinHash signatures — the fraction of the `num_perm` hash slots
    that agree, which is an unbiased estimator of true shingle-set Jaccard similarity."""
    if len(record_a.minhash_values) != len(record_b.minhash_values):
        raise ValueError(
            "cannot compare MinHash signatures computed with different num_perm "
            f"({len(record_a.minhash_values)} vs {len(record_b.minhash_values)})"
        )
    agreements = sum(
        1 for a, b in zip(record_a.minhash_values, record_b.minhash_values, strict=True) if a == b
    )
    return agreements / len(record_a.minhash_values)


class _UnionFind:
    """Same minimal disjoint-set as phash.py's `_UnionFind` — duplicated rather than shared
    because this module must not import from `phash.py` (no coupling between the image and
    document pipelines beyond both living under `reclaim.ai`)."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        root_i, root_j = self.find(i), self.find(j)
        if root_i != root_j:
            self._parent[root_i] = root_j


def cluster_by_jaccard_similarity(
    records: Sequence[DocumentMinHash], *, min_similarity: float
) -> list[list[DocumentMinHash]]:
    """Groups `records` into near-dup clusters: any pair with Jaccard similarity >=
    `min_similarity` is unioned into the same cluster (transitively — same threshold-based
    clustering semantics as `phash.cluster_by_hamming_distance`, just similarity instead of
    distance, and a MINIMUM to clear instead of a MAXIMUM). Singleton "clusters" are dropped.

    `min_similarity` is a provisional operating point until measured on a real dataset — see
    ADR-0017. O(n^2) pairwise comparison, same scale reasoning as
    `phash.cluster_by_hamming_distance`: this runs on the residual after exact-hash dedup.
    """
    union_find = _UnionFind(len(records))
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            if jaccard_similarity(records[i], records[j]) >= min_similarity:
                union_find.union(i, j)

    groups: dict[int, list[DocumentMinHash]] = {}
    for index, record in enumerate(records):
        groups.setdefault(union_find.find(index), []).append(record)
    return [group for group in groups.values() if len(group) > 1]
