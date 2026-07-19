from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

# Loads the PAWS-Wiki "Labeled (Final)" split (ADR-0017) into pair-level ground truth for the
# eval harness. PAWS ("Paraphrase Adversaries from Word Scrambling") is REAL, human/machine-
# labeled, Wikipedia-sourced text — but it is deliberately adversarial BY CONSTRUCTION: its
# negative examples are generated via word-order scrambling and back-translation specifically
# to have HIGH lexical overlap with LOW semantic similarity (the opposite failure mode from
# Copydays' `strong` split, but the same *category* of "constructed to be hard," not "typical
# real-world usage"). This loader is used only as a disclosed secondary/boundary check on the
# sentence-embedding stage — never as the basis for MEASURED status (ADR-0016) — see
# ADR-0017's own primary measurement (evals/test_ai_document_gold.py) for that.


@dataclass(frozen=True, slots=True)
class PawsPair:
    id: int
    sentence1: str
    sentence2: str
    is_paraphrase: bool


def load_paws_labeled_final(parquet_path: Path) -> list[PawsPair]:
    table = pq.read_table(parquet_path)
    return [
        PawsPair(
            id=row["id"],
            sentence1=row["sentence1"],
            sentence2=row["sentence2"],
            is_paraphrase=bool(row["label"]),
        )
        for row in table.to_pylist()
    ]
