from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from ai_fixtures.build_ranker_fixtures import RankerFileRecord

# Feature: generic clutter-likelihood ranker (ADR-0021). Cross-LLM labeling via LOCAL Ollama
# models only — zero paid API, ANTHROPIC_API_KEY never set or read anywhere in this module.
# Judge-call pattern (client construction, deterministic options, retry/backoff, robust JSON
# extraction) follows the same local-judge precedent already proven in the sibling TriageIQ
# project (`triage_iq.evaluation.triage_eval.TriageJudge`) — same `ollama.Client.chat(...,
# think=False, keep_alive=-1, options={"temperature": 0, "seed": 42})` call shape, same
# strip-think-blocks/strip-code-fences/regex-extract-JSON parsing discipline.
#
# ONE deliberate addition beyond the TriageIQ pattern: `num_gpu=0` (force CPU-only inference)
# is REQUIRED here, not optional — measured on this machine, an unrelated process from a
# different project (a conda env named "aetherart", confirmed via `nvidia-smi`) holds ~6.3 of
# this machine's only 8GB of GPU VRAM. Under that real contention, 2 of 3 candidate judge
# models failed outright (a `ResponseError` timeout loading qwen3:8b, a hard crash loading
# llama3.1:8b — `NTSTATUS 0xffffffff`) and the third (gemma2:9b) took ~4 minutes for a
# single one-word response. Forcing CPU-only inference is slower per call (~5-11s once a
# model is warm, vs. sub-second on an uncontended GPU) but reliable — every judge call either
# succeeds deterministically or fails with a clean, retriable error, never a silent GPU-OOM
# hang. This is a real, disclosed environmental constraint of the machine this labeling run
# was actually performed on (see ADR-0021), not a design requirement of the approach itself —
# a future re-run on a machine with a free GPU could drop `num_gpu=0` for much faster labeling
# without changing anything else about this module.

RUBRIC_SYSTEM_PROMPT = (
    "You are labeling files by GENERIC CLUTTER-LIKELIHOOD -- NOT personal preference, NOT "
    "whether any specific person would delete this exact file. Judge only: is this the KIND "
    "of file that is USUALLY safe to flag for cleanup review (build artifacts, stale "
    "installers, cache remnants, temp files, old exports/backups) versus USUALLY important "
    "(personal documents, financial records, source code, credentials/keys, active "
    "configuration)?\n\n"
    "You will be given: file path, extension, size in bytes, age in days (from modification "
    "time), a location classification, a deterministic-engine category (if any detector "
    'already flagged it, "uncategorized" if none did), cluster info (is this file part of a '
    "group of similar/redundant files, and if so is it the recommended keeper?), and "
    "whether it's a cloud-sync placeholder (not fully downloaded locally).\n\n"
    "Grade CLUTTER-LIKELIHOOD on this fixed 0-4 scale:\n"
    "4 = Definite clutter kind: build artifacts, package caches, browser/temp caches, crash "
    "dumps, old installers well past their useful life, duplicate old exports with "
    "generic/versioned names\n"
    "3 = Probable clutter kind: large stale logs, old archives with generic names, a file "
    "clearly inside a large redundant cluster and not the recommended keeper\n"
    "2 = Genuinely ambiguous from the metadata alone -- could reasonably go either way\n"
    "1 = Probably important: personal documents, active project source code, financial "
    "records, personal media\n"
    "0 = Definitely important: credentials/keys, active configuration/secrets, unique "
    "irreplaceable content\n\n"
    "Respond with ONLY a JSON object, no other text: "
    '{"grade": <integer 0-4>, "rationale": "<one short sentence, metadata-based reasoning '
    'only>"}'
)

_MAX_RETRIES = 6
_INITIAL_BACKOFF_SECONDS = 3.0
_MAX_BACKOFF_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class JudgeGrade:
    model: str
    record_id: str
    grade: int  # 0-4, see RUBRIC_SYSTEM_PROMPT
    rationale: str
    raw_response: str


class JudgeCallError(Exception):
    """Every retry exhausted, or the response could never be parsed as a valid grade."""


def _record_to_prompt(record: RankerFileRecord) -> str:
    age_days = round((record.batch_generated_at - record.mtime) / 86400.0, 1)
    cluster = record.cluster_stats
    cluster_desc = (
        "not part of any cluster (a lone file)"
        if cluster.cluster_size <= 1
        else (
            f"in a cluster of {cluster.cluster_size} similar files "
            f"({cluster.score_kind}={cluster.raw_score}), "
            f"{'IS' if cluster.is_recommended_keep else 'is NOT'} the recommended keeper"
        )
    )
    return (
        f"path: {record.path}\n"
        f"extension: {record.ext!r}\n"
        f"size_bytes: {record.size_bytes}\n"
        f"age_days: {age_days}\n"
        f"location_class: {record.path_class}\n"
        f"category: {record.category}\n"
        f"cluster: {cluster_desc}\n"
        f"cloud_sync_placeholder: {record.cloud_sync_flag}"
    )


def _strip_think_blocks(text: str) -> str:
    without_blocks = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return re.sub(r"</?think>", "", without_blocks).strip()


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"```\s*$", "", stripped)
    return stripped.strip()


def _parse_grade(raw: str, *, model: str, record_id: str) -> JudgeGrade:
    cleaned = _strip_code_fences(_strip_think_blocks(raw))
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match is None:
        raise JudgeCallError(f"{model}/{record_id}: no JSON object found in response: {raw!r}")
    try:
        data = json.loads(match.group(0))
        grade = int(data["grade"])
        rationale = str(data.get("rationale", ""))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise JudgeCallError(f"{model}/{record_id}: malformed JSON grade: {raw!r}") from exc
    if not (0 <= grade <= 4):
        raise JudgeCallError(f"{model}/{record_id}: grade {grade} out of the fixed 0-4 range")
    return JudgeGrade(
        model=model, record_id=record_id, grade=grade, rationale=rationale, raw_response=raw
    )


class LocalLLMJudge:
    """One Ollama model acting as an independent clutter-likelihood rater. Zero paid API —
    `ollama.Client` talks only to the local `http://localhost:11434` server; nothing here
    ever reads or sets `ANTHROPIC_API_KEY` or any other cloud credential."""

    def __init__(self, model: str, *, host: str | None = None) -> None:
        import ollama

        self._model = model
        self._client = ollama.Client(host=host) if host else ollama.Client()

    @property
    def model(self) -> str:
        return self._model

    def grade(self, record: RankerFileRecord) -> JudgeGrade:
        messages = [
            {"role": "system", "content": RUBRIC_SYSTEM_PROMPT},
            {"role": "user", "content": _record_to_prompt(record)},
        ]
        options = {"temperature": 0, "seed": 42, "num_gpu": 0}  # see module docstring: CPU-
        # only is a measured, disclosed environmental necessity on this machine, not a
        # design requirement.
        # `think=False` is harmless for non-thinking models (they simply ignore it) and
        # required to get clean, un-prefixed JSON out of Qwen3-family models -- passed
        # uniformly rather than conditionally, which also keeps this a plain call with real
        # keyword arguments instead of a **kwargs splat mypy can't verify against
        # ollama.Client.chat's overloads.

        last_error: Exception | None = None
        backoff = _INITIAL_BACKOFF_SECONDS
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.chat(
                    model=self._model,
                    messages=messages,
                    think=False,
                    keep_alive=-1,
                    options=options,
                )
                content = response["message"]["content"]
                return _parse_grade(content, model=self._model, record_id=record.record_id)
            except Exception as exc:
                # local-server hiccups are the only failure mode (no rate limiting locally);
                # retry any of them, same posture as TriageIQ's `_ollama_completion`.
                last_error = exc
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
        raise JudgeCallError(
            f"{self._model}/{record.record_id}: exhausted {_MAX_RETRIES} retries"
        ) from last_error
