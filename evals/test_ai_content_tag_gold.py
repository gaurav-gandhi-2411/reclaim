from __future__ import annotations

import json
from pathlib import Path

import pytest
from ai_fixtures.build_content_tag_fixtures import build_all_tagged_samples

from reclaim.ai.content_tagger import (
    KEEP_BIASED_TAGS,
    ContentTag,
    score_all_tags,
    tag_content,
)
from reclaim.ai.eval_harness import (
    DistributionDeclaration,
    EvalReport,
    current_commit_sha,
    select_operating_point_per_tier,
)

# Feature 2's content-tag classifier operating point (ADR-0019). Per-tier gating (ADR-0018):
# each of the 5 real tags is its own tier — precision/recall are computed from ONLY that
# tier's own samples, never pooled. GG's explicit instruction ("bias STRONGLY toward keep for
# receipt/document/code tags... only transient-UI may be deletion-eligible") makes TRANSIENT_UI
# precision the one number that's actually SAFETY-critical (a false positive there is the only
# way this classifier can push a non-disposable screenshot toward deletion-eligibility) — every
# other tag's precision/recall is a QUALITY signal only, since misclassifying a document as
# "chat" or "code" keeps it in the keep-biased set either way (never unsafe, just imperfect
# review-UI labeling). This eval measures both: a uniform, per-tier-gated operating point across
# all 5 tags (the ADR-0018-mandated harness call), AND a stricter, explicit, redundant
# TRANSIENT_UI-only precision floor at that operating point (the actual safety property).
#
# Not in the default CI sweep (reads Reclaim's own source tree + requires the Gutenberg corpus,
# same precondition as test_ai_document_templated_gold.py). Reproduce with:
#   uv run python evals/ai_fixtures/fetch_gutenberg_texts.py
#   uv run pytest evals/test_ai_content_tag_gold.py -v -s

_GUTENBERG_ROOT = Path("data/ai_datasets/gutenberg_texts/cleaned")
_TRANSIENT_UI_PRECISION_FLOOR = 0.95  # the safety-critical number (GG's "bias STRONGLY toward
# keep" instruction) — deliberately much stricter than the uniform floor below.
_UNIFORM_TARGET_PRECISION = 0.65
_UNIFORM_MIN_RECALL = 0.5
_COMMAND = "uv run pytest evals/test_ai_content_tag_gold.py -v -s"
_FIXTURE = (
    "data/ai_datasets/gutenberg_texts (document) + src/reclaim/** (code) + "
    "evals/ai_fixtures/build_content_tag_fixtures.py (receipt/chat/transient_ui, synthetic)"
)
_REPORT_PATH = Path("reports/ai/content_tag_operating_point.json")

_DISTRIBUTION = DistributionDeclaration(
    description=(
        "5-tag content classification: DOCUMENT from real public-domain Gutenberg prose "
        "(screenshot-length snippets), CODE from real Reclaim source files, RECEIPT/CHAT/"
        "TRANSIENT_UI synthetic-but-structurally-realistic (no plausible public dataset "
        "exists for this exact screenshot-content taxonomy — see ADR-0019), plus a small "
        "UNKNOWN pool of genuinely ambiguous OCR-noise-like strings included as negatives for "
        "every real tag."
    ),
    is_realistic=False,
    is_adversarial_tail_only=False,
    is_synthetic_only=True,  # 3 of 5 tags (+ the UNKNOWN stress pool) are synthetic; the whole
    # distribution's TAG FREQUENCIES are not sampled from real screenshot telemetry (no such
    # ground truth exists) — honestly PROVISIONAL, never promoted to MEASURED (see
    # assert_safe_to_promote_to_measured's docstring; deliberately NOT called in this eval).
    untested_variation_note=(
        "Real screenshots (photographed receipts at an angle, low-confidence partial OCR, "
        "code in non-Python languages/dark-mode IDE themes, chat apps with very different UI "
        "chrome than modeled here, non-English text) are not covered. Document/code/chat/"
        "receipt confusion among themselves (all keep-biased, so safe) is measured and "
        "disclosed but not gated as strictly as TRANSIENT_UI precision, which IS the safety-"
        "critical number this eval exists to prove."
    ),
)

pytestmark = pytest.mark.skipif(
    not _GUTENBERG_ROOT.exists() or not any(_GUTENBERG_ROOT.glob("*.txt")),
    reason=(
        "Gutenberg texts not present locally — run "
        "`uv run python evals/ai_fixtures/fetch_gutenberg_texts.py` first."
    ),
)


_QUALITY_TAGS = (ContentTag.RECEIPT, ContentTag.CODE, ContentTag.CHAT, ContentTag.DOCUMENT)
_ALL_REAL_TAGS = (*_QUALITY_TAGS, ContentTag.TRANSIENT_UI)


def _build_tiers(
    predictions: list[tuple[str, ContentTag, float]], tags: tuple[ContentTag, ...]
) -> dict[str, tuple[list[float], list[float]]]:
    """Per tag T: positive_scores holds T's own predicted-confidence for every truly-T
    sample T actually won (argmax) as, else -inf (can never cross any threshold) — same for
    negative_scores over non-T samples. This correctly reconstructs "would this sample be
    classified as T at threshold θ" from `score_all_tags`' raw scores without re-implementing
    `tag_content`'s argmax-then-threshold logic a second time."""
    tiers: dict[str, tuple[list[float], list[float]]] = {}
    for tag in tags:
        positive_scores = [
            conf if predicted == tag else float("-inf")
            for true_tag, predicted, conf in predictions
            if true_tag == tag.value
        ]
        negative_scores = [
            conf if predicted == tag else float("-inf")
            for true_tag, predicted, conf in predictions
            if true_tag != tag.value
        ]
        tiers[tag.value] = (positive_scores, negative_scores)
    return tiers


def test_content_tag_operating_point_per_tier_and_transient_ui_safety_floor() -> None:
    samples = build_all_tagged_samples()
    assert len(samples) > 200, f"expected a substantial fixture pool, got {len(samples)}"

    predictions: list[tuple[str, ContentTag, float]] = []
    for sample in samples:
        scores = score_all_tags(sample.text)
        predicted_tag = max(scores, key=lambda tag: scores[tag])
        predictions.append((sample.true_tag, predicted_tag, scores[predicted_tag]))

    candidates = [round(0.05 * n, 2) for n in range(0, 60)]  # 0.00 .. 2.95

    # --- Diagnostic 1: the 4 "quality-only" tags (misclassification among them, or with
    # UNKNOWN, is always safe -- still keep-biased either way) at a realistic, achievable
    # uniform floor. Per-tier gated (ADR-0018), never pooled. ---
    quality_operating_point = select_operating_point_per_tier(
        _build_tiers(predictions, _QUALITY_TAGS),
        candidates=candidates,
        target_precision=_UNIFORM_TARGET_PRECISION,
        min_recall=_UNIFORM_MIN_RECALL,
        distribution=_DISTRIBUTION,
        source_description=(
            f"content-tag classifier, quality tags only, commit {current_commit_sha()}"
        ),
    )
    assert quality_operating_point is not None, (
        "no confidence threshold clears the realistic uniform floor on the 4 keep-biased-"
        "either-way tags — a real, reportable finding about the classical v1 scorer's limits"
    )

    # --- Diagnostic 2: TRANSIENT_UI alone, at its own much stricter floor -- proves a
    # threshold EXISTS that satisfies the actual safety property. A single-tier call is still
    # a legitimate per-tier-gated call (ADR-0018 forbids POOLING tiers, not measuring one
    # alone), never a pooled/aggregate number. ---
    safety_operating_point = select_operating_point_per_tier(
        _build_tiers(predictions, (ContentTag.TRANSIENT_UI,)),
        candidates=candidates,
        target_precision=_TRANSIENT_UI_PRECISION_FLOOR,
        min_recall=_UNIFORM_MIN_RECALL,
        distribution=_DISTRIBUTION,
        source_description=(
            f"content-tag classifier, transient_ui safety tier, commit {current_commit_sha()}"
        ),
    )
    assert safety_operating_point is not None, (
        "no confidence threshold clears the strict TRANSIENT_UI-only safety floor at all — "
        "this would mean the classifier cannot be shipped safely at any threshold"
    )
    print(  # noqa: T201
        f"\nDiagnostic thresholds: quality={quality_operating_point.threshold} "
        f"safety={safety_operating_point.threshold}"
    )

    # --- THE ACTUAL GATE: the classifier AS SHIPPED TODAY (tag_content, its real fixed
    # `_MIN_CONFIDENT_SCORE`), not a swept threshold -- this is what actually runs in
    # production. Both diagnostics above prove achievable ranges exist; this proves the
    # shipped constant falls inside the safe range. ---
    shipped_tags = [(sample.true_tag, tag_content(sample.text).tag) for sample in samples]
    per_tag_report: dict[str, dict[str, float]] = {}
    for tag in _ALL_REAL_TAGS:
        true_count = sum(1 for t, _ in shipped_tags if t == tag.value)
        tp = sum(1 for t, p in shipped_tags if t == tag.value and p == tag)
        fp = sum(1 for t, p in shipped_tags if t != tag.value and p == tag)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / true_count if true_count else 0.0
        per_tag_report[tag.value] = {"precision": precision, "recall": recall}

    print("\nSHIPPED classifier (tag_content, real _MIN_CONFIDENT_SCORE) per-tag metrics:")  # noqa: T201
    for tag_value, metrics in per_tag_report.items():
        precision, recall = metrics["precision"], metrics["recall"]
        print(f"  tag={tag_value:14s} precision={precision:.4f} recall={recall:.4f}")  # noqa: T201

    for tag in _QUALITY_TAGS:
        metrics = per_tag_report[tag.value]
        assert metrics["precision"] >= _UNIFORM_TARGET_PRECISION, tag
        assert metrics["recall"] >= _UNIFORM_MIN_RECALL, tag

    transient_ui_shipped = per_tag_report[ContentTag.TRANSIENT_UI.value]
    assert transient_ui_shipped["precision"] >= _TRANSIENT_UI_PRECISION_FLOOR, (
        "TRANSIENT_UI is the ONLY deletion-eligible content tag (GG's explicit instruction) — "
        "the SHIPPED classifier's precision on it must clear a much stricter floor than the "
        "other (keep-biased-either-way) tags, since a false positive here is the one way this "
        "classifier can push a non-disposable screenshot toward deletion-eligibility"
    )

    # --- Sanity: the strongest possible statement of the safety property -- at the SHIPPED
    # threshold, NO real (non-transient-ui, non-unknown) content sample is EVER classified as
    # transient-UI, not just "precision happens to be high." ---
    dangerous_false_positives = [
        sample.sample_id
        for sample, (true_tag, predicted) in zip(samples, shipped_tags, strict=True)
        if predicted == ContentTag.TRANSIENT_UI
        and true_tag not in {ContentTag.TRANSIENT_UI.value, ContentTag.UNKNOWN.value}
    ]
    assert dangerous_false_positives == [], (
        f"a real-content sample (receipt/document/code/chat) was classified as the "
        f"deletion-eligible TRANSIENT_UI tag by the shipped classifier: {dangerous_false_positives}"
    )

    report = EvalReport(
        metric_name="content_tag_transient_ui_precision_shipped",
        value=transient_ui_shipped["precision"],
        commit_sha=current_commit_sha(),
        command=_COMMAND,
        fixture_path=_FIXTURE,
    )
    print(f"\n{report}")  # noqa: T201

    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(
        json.dumps(
            {
                "commit_sha": current_commit_sha(),
                "command": _COMMAND,
                "fixture_path": _FIXTURE,
                "shipped_per_tag": per_tag_report,
                "diagnostic_quality_threshold": quality_operating_point.threshold,
                "diagnostic_safety_threshold": safety_operating_point.threshold,
                "sample_counts": {
                    tag.value: sum(1 for t, _ in shipped_tags if t == tag.value)
                    for tag in (*_ALL_REAL_TAGS, ContentTag.UNKNOWN)
                },
                "keep_biased_tags": sorted(t.value for t in KEEP_BIASED_TAGS),
                "note": (
                    "Provisional (synthetic-only distribution, ADR-0019) — never promoted to "
                    "MEASURED. transient_ui precision is the safety-critical gate; other tags' "
                    "precision/recall are quality signals only, since any misclassification "
                    "among them stays keep-biased and never becomes deletion-eligible."
                ),
            },
            indent=2,
        )
    )
    print(f"\nFull report: {_REPORT_PATH}")  # noqa: T201
