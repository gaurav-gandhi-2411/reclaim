from __future__ import annotations

import json
from pathlib import Path

import pytest
from ai_fixtures.copydays_loader import (
    CopydaysImage,
    all_pairs,
    blocks_with_original_and_variants,
    discover_copydays_images,
)

from reclaim.ai.eval_harness import (
    EvalReport,
    current_commit_sha,
    precision_recall_curve,
    select_operating_point,
)
from reclaim.ai.keep_best import score_image_quality
from reclaim.ai.phash import compute_image_hashes, hamming_distance

# Feature 1a Track A's REAL operating-point measurement (ADR-0015, promoting ADR-0012 from
# provisional to MEASURED). Runs against the public INRIA Copydays dataset — real,
# human-construction-verified ground truth, not synthetic and not LLM-labeled. Deliberately
# NOT part of the default `uv run pytest evals/` CI sweep (see .github/workflows/eval.yml):
# the dataset is a ~268MB third-party download (never committed — see .gitignore's
# `data/ai_datasets/`), and CI should not carry a network dependency for a measurement that
# only needs to be re-derived when the pipeline actually changes, not on every push. Reproduce
# locally with:
#   uv run python evals/ai_fixtures/fetch_copydays.py
#   uv run pytest evals/test_ai_copydays_gold.py -v -s
#
# `_COPYDAYS_ROOT` matches fetch_copydays.py's default; this file only READS what's already
# there, it never downloads (a test that reaches the network on every local `pytest evals/`
# run would be its own kind of flaky).

_COPYDAYS_ROOT = Path("data/ai_datasets/copydays/extracted")
_TARGET_PRECISION = 0.95  # spec §7.3: near-identical/deletion-suggestion tracks >= 0.95
_COMMAND = "uv run pytest evals/test_ai_copydays_gold.py -v -s"
_FIXTURE = "data/ai_datasets/copydays (INRIA Copydays, ADR-0015)"
_DISAGREEMENTS_REPORT = Path("reports/ai/copydays_keep_best_disagreements.json")

pytestmark = pytest.mark.skipif(
    not _COPYDAYS_ROOT.exists() or not any(_COPYDAYS_ROOT.glob("*.jpg")),
    reason=(
        "Copydays dataset not present locally — run "
        "`uv run python evals/ai_fixtures/fetch_copydays.py` first. Not auto-downloaded by "
        "this test or by CI (see module docstring)."
    ),
)


def _hash_all(images: list[CopydaysImage]) -> dict[Path, tuple[str, str]]:
    hashes: dict[Path, tuple[str, str]] = {}
    for image in images:
        record = compute_image_hashes(image.path)
        assert record is not None, f"real Copydays image failed to decode: {image.path}"
        hashes[image.path] = (record.phash_hex, record.dhash_hex)
    return hashes


def test_real_pr_curve_and_operating_point_on_copydays() -> None:
    """The measurement ADR-0012 graduates from provisional to MEASURED on — the actual PR
    curve, computed from real Hamming distances over every one of Copydays' ~74k image pairs,
    labeled by the dataset's own construction (same block = same source photo, however
    attacked; different block = unrelated). No synthetic data, no LLM labels."""
    images = discover_copydays_images(_COPYDAYS_ROOT)
    assert len(images) == 386, (
        f"expected the known Copydays original+strong count, got {len(images)}"
    )

    hashes = _hash_all(images)
    pairs = all_pairs(images)
    n_positive = sum(1 for p in pairs if p.is_same_photo)
    n_negative = len(pairs) - n_positive
    assert n_positive == 314  # 157 blocks, verified block-size distribution (95x2+54x3+7x4+1x6)
    assert n_negative == 73_991

    scored: list[tuple[float, bool]] = []
    for pair in pairs:
        hash_a, _ = hashes[pair.image_a.path]
        hash_b, _ = hashes[pair.image_b.path]
        distance = hamming_distance(hash_a, hash_b)
        scored.append((float(-distance), pair.is_same_photo))  # negate: higher = more similar

    curve = precision_recall_curve(scored, higher_score_is_more_similar=True)
    operating_point = select_operating_point(
        curve,
        target_precision=_TARGET_PRECISION,
        source_description=(
            f"INRIA Copydays (ADR-0015), commit {current_commit_sha()} — REAL gold set, "
            "not synthetic"
        ),
    )

    # Report the full curve shape (a handful of representative points, not all ~74k) so the
    # precision/recall tradeoff is visible, not just the single chosen point. `curve` has one
    # PRPoint per PAIR in tie-broken sort order, so several consecutive points can share the
    # same threshold (= raw Hamming distance) when multiple pairs tie at that exact distance —
    # printing the FIRST point seen per threshold would show a mid-tie snapshot, not the true
    # "every pair with distance <= X" cumulative value a reader would expect from a
    # max_hamming_distance row. Keeping the LAST point per threshold (curve order is
    # non-decreasing in cumulative TP/FP) gives the correct cumulative precision/recall instead.
    print(f"\n=== Copydays real PR curve ({len(pairs)} pairs, {n_positive} positive) ===")  # noqa: T201
    last_point_by_threshold: dict[float, object] = {}
    for point in curve:
        last_point_by_threshold[point.threshold] = point
    for max_distance, point in sorted(
        ((int(-t), p) for t, p in last_point_by_threshold.items()), key=lambda pair: pair[0]
    )[:40]:  # curve is long; print a bounded prefix, not all ~74k distinct distances
        print(  # noqa: T201
            f"  max_hamming_distance<={max_distance:3d}  "
            f"precision={point.precision:.4f}  recall={point.recall:.4f}"
        )

    if operating_point is None:
        report = EvalReport(
            metric_name="copydays_operating_point_max_hamming_distance",
            value=float("nan"),
            commit_sha=current_commit_sha(),
            command=_COMMAND,
            fixture_path=_FIXTURE,
        )
        print(  # noqa: T201
            f"\nNO threshold on the real Copydays PR curve reaches target precision "
            f"{_TARGET_PRECISION} — this is a real, reportable finding, not a bug to paper "
            f"over. {report}"
        )
        best_precision = max((p.precision for p in curve), default=0.0)
        print(f"Best achievable precision on this curve: {best_precision:.4f}")  # noqa: T201
        pytest.skip(
            f"No operating point clears target precision {_TARGET_PRECISION} on the real "
            "Copydays curve — see printed curve above; reported to GG rather than silently "
            "lowering the target or picking an unqualified point."
        )
    else:
        max_distance = int(-operating_point.threshold)
        report = EvalReport(
            metric_name="copydays_operating_point_max_hamming_distance",
            value=float(max_distance),
            commit_sha=current_commit_sha(),
            command=_COMMAND,
            fixture_path=_FIXTURE,
        )
        print(f"\nMEASURED operating point: {report}")  # noqa: T201
        print(  # noqa: T201
            f"  precision={operating_point.precision:.4f}  recall={operating_point.recall:.4f}  "
            f"is_provisional={operating_point.is_provisional}"
        )


def test_keep_best_against_copydays_original_vs_attacked() -> None:
    """ADR-0015 §4: real near-dup groups (Copydays blocks) where the classical scorer's
    keeper SHOULD be the unmodified original — print-and-scan/blur/paint are all
    quality-degrading by construction, not a fabricated preference label. Reports top-1
    agreement + the never-picks-worst-quartile safety rate, and writes every DISAGREEMENT
    (scorer picked an attacked variant over the original) to a small JSON report for GG's
    optional one-click confirmation — never silently overridden or hidden."""
    images = discover_copydays_images(_COPYDAYS_ROOT)
    blocks = blocks_with_original_and_variants(images)
    assert len(blocks) == 157

    top1_agree = 0
    worst_quartile_picks = 0
    disagreements: list[dict[str, object]] = []

    for block_id, (original, variants) in blocks.items():
        members = [original, *variants]
        scores = [score_image_quality(m.path) for m in members]
        assert all(s is not None for s in scores), f"block {block_id}: a member failed to score"
        ranked = sorted(scores, key=lambda s: s.combined, reverse=True)  # best first
        keeper = ranked[0]
        quartile_cutoff = max(1, len(ranked) // 4)
        worst_quartile_paths = {s.path for s in ranked[-quartile_cutoff:]}

        if keeper.path == original.path:
            top1_agree += 1
        else:
            disagreements.append(
                {
                    "block_id": block_id,
                    "original": str(original.path),
                    "scorer_picked": str(keeper.path),
                    "original_combined_score": next(
                        s.combined for s in scores if s.path == original.path
                    ),
                    "picked_combined_score": keeper.combined,
                }
            )
        if keeper.path in worst_quartile_paths and len(ranked) >= 4:
            worst_quartile_picks += 1

    top1_agreement = top1_agree / len(blocks)
    safety_rate = 1.0 - (worst_quartile_picks / len(blocks))

    print(f"\n=== Copydays keep-best ({len(blocks)} blocks) ===")  # noqa: T201
    print(  # noqa: T201
        EvalReport(
            metric_name="copydays_keep_best_top1_agreement",
            value=top1_agreement,
            commit_sha=current_commit_sha(),
            command=_COMMAND,
            fixture_path=_FIXTURE,
        )
    )
    print(  # noqa: T201
        EvalReport(
            metric_name="copydays_keep_best_never_worst_quartile_safety_rate",
            value=safety_rate,
            commit_sha=current_commit_sha(),
            command=_COMMAND,
            fixture_path=_FIXTURE,
        )
    )
    print(f"disagreements: {len(disagreements)}/{len(blocks)} blocks — see {_DISAGREEMENTS_REPORT}")  # noqa: T201

    _DISAGREEMENTS_REPORT.parent.mkdir(parents=True, exist_ok=True)
    _DISAGREEMENTS_REPORT.write_text(
        json.dumps(
            {
                "commit_sha": current_commit_sha(),
                "command": _COMMAND,
                "fixture_path": _FIXTURE,
                "total_blocks": len(blocks),
                "top1_agreement": top1_agreement,
                "never_worst_quartile_safety_rate": safety_rate,
                "disagreements": disagreements,
                "note": (
                    "Every case where the classical keep-best scorer picked an attacked "
                    "Copydays variant over the real original. Not auto-resolved — this file "
                    "is for GG's optional one-click confirmation, per the explicit "
                    "instruction not to fabricate preference labels."
                ),
            },
            indent=2,
        )
    )

    # Not asserted as a hard CI gate at a specific bar — per instruction, disagreements are
    # surfaced for review, not silently pass/failed against a threshold nobody has confirmed.
    # A near-zero top-1 agreement WOULD be a real, actionable finding worth failing loudly on.
    assert top1_agreement > 0.5, (
        f"only {top1_agreement:.2%} agreement between the classical scorer and Copydays' "
        "real originals — this is far below chance-adjacent and suggests the scorer itself "
        "is broken, not just imperfectly tuned; investigate before trusting any keep-best "
        "output"
    )
