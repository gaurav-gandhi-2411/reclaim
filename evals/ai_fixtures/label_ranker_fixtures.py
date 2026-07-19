from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_fixtures.build_ranker_fixtures import RankerFileRecord, build_all_ranker_records
from ai_fixtures.ranker_llm_judge import JudgeCallError, LocalLLMJudge

# Feature: generic clutter-likelihood ranker (ADR-0021). ONE-TIME (re-runnable, deterministic
# given the same fixture + models) cross-LLM labeling pass — the slow step (real local Ollama
# calls, ~1.75 hours measured on this machine under CPU-only inference, see
# ranker_llm_judge.py's module docstring for why CPU-only is required here), separated from
# the fast, repeatable eval that reads its cached output (mirrors fetch_gutenberg_texts.py /
# fetch_copydays.py's split between "slow real-data acquisition" and "fast eval against the
# cached result").
#
# Output: data/ai_datasets/ranker_labels/labeled_records.jsonl (gitignored — large,
# regeneratable from this script + the fixture generator + the same model versions, not
# copyrighted/personal data but still not worth committing) and
# reports/ai/ranker_labeling_kappa.json (committed — the provenance-tracked summary: measured
# Fleiss' kappa, per-pair Cohen's kappa, exclusion rate).
#
# Zero paid API anywhere in this file or anything it imports — every judge call goes through
# `ranker_llm_judge.LocalLLMJudge`, which only ever talks to a local Ollama server.

_JUDGE_MODELS = ("qwen3:8b", "llama3.1:8b", "gemma2:9b")
_OUTPUT_DIR = Path("data/ai_datasets/ranker_labels")
_LABELED_RECORDS_PATH = _OUTPUT_DIR / "labeled_records.jsonl"
_REPORT_PATH = Path("reports/ai/ranker_labeling_kappa.json")


def _record_to_dict(record: RankerFileRecord) -> dict[str, object]:
    data = asdict(record)
    return data


def label_all_records(
    records: list[RankerFileRecord], *, models: tuple[str, ...] = _JUDGE_MODELS
) -> dict[str, dict[str, int]]:
    """Returns `{record_id: {model_name: grade}}` — every record graded by every model, one
    model loaded/kept-warm at a time (not interleaved per-record) so Ollama's `keep_alive=-1`
    actually keeps the SAME model warm across consecutive calls, rather than reloading a
    different model from disk every single call.
    """
    grades_by_record: dict[str, dict[str, int]] = {record.record_id: {} for record in records}
    for model in models:
        judge = LocalLLMJudge(model)
        start = time.time()
        for index, record in enumerate(records):
            try:
                grade = judge.grade(record)
            except JudgeCallError as exc:
                print(f"  [{model}] record {record.record_id} FAILED: {exc}")  # noqa: T201
                continue
            grades_by_record[record.record_id][model] = grade.grade
            if (index + 1) % 20 == 0:
                elapsed = time.time() - start
                print(  # noqa: T201
                    f"  [{model}] {index + 1}/{len(records)} records "
                    f"({elapsed:.0f}s elapsed, {elapsed / (index + 1):.1f}s/record avg)"
                )
        print(f"[{model}] done: {time.time() - start:.0f}s total")  # noqa: T201
    return grades_by_record


def main() -> None:
    records = build_all_ranker_records()
    print(f"Labeling {len(records)} records with {len(_JUDGE_MODELS)} local judges...")  # noqa: T201

    grades_by_record = label_all_records(records)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with _LABELED_RECORDS_PATH.open("w", encoding="utf-8") as fh:
        for record in records:
            row = {
                "record": _record_to_dict(record),
                "grades": grades_by_record[record.record_id],
            }
            fh.write(json.dumps(row))
            fh.write("\n")

    num_judges = len(_JUDGE_MODELS)
    complete_records = [r for r in records if len(grades_by_record[r.record_id]) == num_judges]
    print(  # noqa: T201
        f"\n{len(complete_records)}/{len(records)} records got a grade from all "
        f"{len(_JUDGE_MODELS)} judges (the rest had at least one judge call fail after "
        "retries -- excluded from the agreement/training set entirely, same as a "
        "disagreement exclusion)."
    )

    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(
        json.dumps(
            {
                "models": list(_JUDGE_MODELS),
                "total_records": len(records),
                "complete_records": len(complete_records),
                "labeled_records_path": str(_LABELED_RECORDS_PATH),
            },
            indent=2,
        )
    )
    print(f"\nWrote {_LABELED_RECORDS_PATH} and {_REPORT_PATH}")  # noqa: T201


if __name__ == "__main__":
    main()
