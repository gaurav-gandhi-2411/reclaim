from __future__ import annotations

from pathlib import Path

from ai_fixtures.build_version_chain_fixtures import (
    materialize_version_chain_fixtures,
    true_order_for,
)

from reclaim.ai.eval_harness import (
    EvalReport,
    current_commit_sha,
    exact_order_accuracy,
    kendall_tau,
)
from reclaim.ai.version_chain import order_version_chain

# Feature 1b's version-chain ordering eval (spec §7.2: exact-order accuracy + Kendall's tau).
# Against a constructed, disclosed fixture set (see build_version_chain_fixtures.py's
# docstring for why a public dataset isn't the right source for this Reclaim-specific
# filename-convention problem). This IS the "real" measurement for this sub-feature — there is
# no separate "realistic distribution" follow-up the way pHash/MinHash needed, because the
# fixture already models real-world filename conventions directly (not an adversarial/hard-
# only subset the way Copydays' `strong` split was).

_COMMAND = "uv run pytest evals/test_ai_version_chain_gate.py -v -s"
_FIXTURE = "evals/ai_fixtures/build_version_chain_fixtures.py"


def test_order_version_chain_exact_accuracy_and_kendall_tau(tmp_path: Path) -> None:
    chains = materialize_version_chain_fixtures(tmp_path)
    assert len(chains) == 8

    exact_scores: list[float] = []
    tau_scores: list[float] = []
    failures: list[str] = []
    for chain_id, scrambled_paths in chains:
        predicted = order_version_chain(scrambled_paths)
        true_paths = true_order_for(chain_id, tmp_path)

        predicted_names = [p.name for p in predicted]
        true_names = [p.name for p in true_paths]

        exact = exact_order_accuracy(predicted_names, true_names)
        tau = kendall_tau(predicted_names, true_names)
        exact_scores.append(exact)
        tau_scores.append(tau)
        if exact < 1.0:
            failures.append(
                f"{chain_id}: predicted={predicted_names} true={true_names} tau={tau:.4f}"
            )

    mean_exact_accuracy = sum(exact_scores) / len(exact_scores)
    mean_kendall_tau = sum(tau_scores) / len(tau_scores)

    print(f"\n=== Version-chain ordering ({len(chains)} chains) ===")  # noqa: T201
    for chain_id, exact, tau in zip([c[0] for c in chains], exact_scores, tau_scores, strict=True):
        print(f"  {chain_id:38s} exact={exact:.1f} tau={tau:.4f}")  # noqa: T201
    if failures:
        print("\nFailures:")  # noqa: T201
        for failure in failures:
            print(f"  {failure}")  # noqa: T201

    print(  # noqa: T201
        EvalReport(
            metric_name="version_chain_mean_exact_order_accuracy",
            value=mean_exact_accuracy,
            commit_sha=current_commit_sha(),
            command=_COMMAND,
            fixture_path=_FIXTURE,
        )
    )
    print(  # noqa: T201
        EvalReport(
            metric_name="version_chain_mean_kendall_tau",
            value=mean_kendall_tau,
            commit_sha=current_commit_sha(),
            command=_COMMAND,
            fixture_path=_FIXTURE,
        )
    )

    # CI regression floor — not a target-precision-style operating point (there's no
    # threshold to select; the heuristic either orders correctly or it doesn't), but a floor
    # below which the heuristic needs real investigation, same posture as ADR-0012's BCubed
    # floor test.
    assert mean_exact_accuracy >= 0.75, (
        f"version-chain exact-order accuracy {mean_exact_accuracy:.4f} is below the 0.75 "
        "floor -- the filename-pattern heuristic needs investigation, not a threshold tweak"
    )
    assert mean_kendall_tau >= 0.8
