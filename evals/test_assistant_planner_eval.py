from __future__ import annotations

import json
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from reclaim.mcp.planner import Constraints, FreeSpacePlan, plan_free_space

# Layer 1 of the assistant eval (docs/specs/assistant-mcp.md section 8): `plan_free_space`
# against 60 fixture cases (40 feasible over five synthetic machines, 10 infeasible, 10
# adversarial), with no model involved. The gold answers come from brute force in the builder,
# a different algorithm from the planner's knapsack.
#
# HARD ZERO (release gate): any unsafe selection in any case fails the suite whatever the other
# metrics say. Unsafe = a Tier B selection without max_tier="both", a selection in an excluded
# category, outside include_categories, with a member matching an excluded glob, or a plan over
# max_items. Rates are reported with n and a Wilson 95% interval, never bare.

CASES = Path(__file__).parent / "fixtures" / "assistant_mcp"
BUILDER = CASES / "build_planner_cases.py"
PLAN_TOLERANCE = 0.001  # sizes are rounded to target/4096 in the search: <=0.1% of target


@dataclass(frozen=True)
class FixtureCandidate:
    category: str
    category_group: str
    size_bytes: int


def _cases() -> list[dict[str, object]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES.glob("case_*.json"))]


def _triples(case: dict[str, object]) -> list[tuple[FixtureCandidate, str, str]]:
    out = []
    for s in case["selections"]:  # type: ignore[attr-defined]
        for path, size in zip(s["paths"], s["sizes"], strict=True):
            out.append((FixtureCandidate(s["rule"], s["group"], size), s["tier"], path))
    return out


def _constraints(case: dict[str, object]) -> Constraints:
    c = dict(case["constraints"])  # type: ignore[call-overload]
    return Constraints(
        max_tier=c.get("max_tier", "A"),
        include_categories=tuple(c.get("include_categories", ())),
        exclude_categories=tuple(c.get("exclude_categories", ())),
        exclude_path_globs=tuple(c.get("exclude_path_globs", ())),
        max_items=c.get("max_items"),
        prefer=c.get("prefer", "lowest_risk"),
    )


def _unsafe(case: dict[str, object], plan: FreeSpacePlan) -> list[str]:
    c = _constraints(case)
    groups = {(s["rule"], s["tier"]): s for s in case["selections"]}  # type: ignore[attr-defined]
    bad = []
    for sel in plan.selections:
        s = groups[(sel.rule_id_or_category, sel.tier)]
        name = f"{sel.rule_id_or_category}/{sel.tier}"
        if sel.tier == "B" and c.max_tier != "both":
            bad.append(f"{name}: tier B not allowed")
        if sel.rule_id_or_category in c.exclude_categories or s["group"] in c.exclude_categories:
            bad.append(f"{name}: excluded category")
        if c.include_categories and not (
            sel.rule_id_or_category in c.include_categories or s["group"] in c.include_categories
        ):
            bad.append(f"{name}: outside include_categories")
        if any("keepme" in p.lower() for p in s["paths"]) and c.exclude_path_globs:
            bad.append(f"{name}: a member matches an excluded glob")
        if sel.paths != tuple(sorted(s["paths"])) or sel.bytes != sum(s["sizes"]):
            bad.append(f"{name}: not the whole selection")
    if c.max_items is not None and sum(s.item_count for s in plan.selections) > c.max_items:
        bad.append("over max_items")
    return bad


def _wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    z = 1.959964
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (centre - half, centre + half)


def _largest_first_baseline(case: dict[str, object]) -> tuple[int, int]:
    """Trivial rung of the baseline ladder: whole selections, largest first, no constraints."""
    target = int(case["target_bytes"])  # type: ignore[call-overload]
    total = 0
    unsafe = 0
    for s in sorted(case["selections"], key=lambda s: -sum(s["sizes"])):  # type: ignore[attr-defined]
        if total >= target:
            break
        total += sum(s["sizes"])
        if s["tier"] == "B" or any("keepme" in p.lower() for p in s["paths"]):
            unsafe += 1
    return total, unsafe


def test_the_fixtures_are_current() -> None:
    """The committed cases are exactly what the seeded builder writes."""
    before = {p.name: p.read_bytes() for p in CASES.glob("case_*.json")}
    assert len(before) == 60
    subprocess.run([sys.executable, str(BUILDER)], check=True, capture_output=True)  # noqa: S603
    after = {p.name: p.read_bytes() for p in CASES.glob("case_*.json")}
    assert after == before, "re-run build_planner_cases.py and commit the result"


@pytest.mark.parametrize("case", _cases(), ids=lambda c: f"{c['case']}-{c['kind']}")
def test_no_plan_is_ever_unsafe(case: dict[str, object]) -> None:
    plan = plan_free_space(_triples(case), int(case["target_bytes"]), _constraints(case))  # type: ignore[call-overload]
    assert _unsafe(case, plan) == []


def test_planner_quality_against_the_brute_force_oracle(capsys: pytest.CaptureFixture[str]) -> None:
    cases = _cases()
    optimal = feasible_hits = infeasible_ok = constraint_ok = unsafe_total = 0
    n_optimal = n_feasible = n_infeasible = 0
    base_hits = base_unsafe = reachable = 0
    for case in cases:
        target = int(case["target_bytes"])  # type: ignore[call-overload]
        plan = plan_free_space(_triples(case), target, _constraints(case))
        bad = _unsafe(case, plan)
        unsafe_total += len(bad)
        constraint_ok += not bad
        gold = case["gold_planned_bytes"]
        if case["feasible"]:
            n_feasible += 1
            feasible_hits += plan.reaches_target and plan.planned_bytes <= 1.05 * target
            if gold is not None:
                n_optimal += 1
                optimal += abs(plan.planned_bytes - int(gold)) <= PLAN_TOLERANCE * target  # type: ignore[call-overload]
        else:
            n_infeasible += 1
            infeasible_ok += (not plan.reaches_target) and plan.shortfall_bytes > 0
        best = case["best_possible_bytes"]
        if case["feasible"] and best is not None:
            reachable += int(best) <= 1.05 * target  # type: ignore[call-overload]
        b_total, b_unsafe = _largest_first_baseline(case)
        base_unsafe += b_unsafe
        base_hits += bool(case["feasible"]) and target <= b_total <= 1.05 * target
    lo, hi = _wilson(optimal, n_optimal)
    flo, fhi = _wilson(feasible_hits, n_feasible)
    with capsys.disabled():
        print(  # noqa: T201 -- the eval report
            f"\nplanner, n={len(cases)}: optimal vs brute force {optimal}/{n_optimal} "
            f"[{lo:.2f}, {hi:.2f}]; feasible within 5% {feasible_hits}/{n_feasible} "
            f"[{flo:.2f}, {fhi:.2f}] (any whole-selection plan could: {reachable}/{n_feasible}); "
            f"infeasible reported {infeasible_ok}/{n_infeasible}; "
            f"constraint adherence {constraint_ok}/{len(cases)}; unsafe selections "
            f"{unsafe_total}\nbaseline largest-first, no constraints: within 5% "
            f"{base_hits}/{n_feasible}; unsafe selections {base_unsafe}"
        )
    assert unsafe_total == 0  # hard zero
    assert optimal == n_optimal  # exact optimum of the promised objective on every case
    assert infeasible_ok == n_infeasible
    assert constraint_ok == len(cases)
