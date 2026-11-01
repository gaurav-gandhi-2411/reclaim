"""`plan_free_space`: which of Reclaim's own selections free a requested number of bytes.

docs/specs/assistant-mcp.md section 4. A pure, deterministic function of the candidate list
(already filtered by the safety validator, so every exclusion has been applied before anything
reaches this module), the target and the constraints: no index, no disk, no clock, no model.
That is what makes it testable without an LLM and what the layer-1 eval
(`evals/test_assistant_planner_eval.py`) measures.

A selection is what `delete` can execute: one detector rule (`Candidate.category`) at one tier,
taken whole. The assistant cannot name paths (spec principle 2), so a selection can be dropped
from a plan but never trimmed. Constraints only narrow (spec I3, I6): a target the constraints
cannot reach is reported with its shortfall, never met by relaxing one.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

Prefer = Literal["lowest_risk", "fewest_items", "largest_first"]

# Lowest first. Rebuildable groups (models.REBUILDABLE_CATEGORY_GROUPS) come before anything that
# may need a re-download or holds the user's own data; duplicates last among Tier A because a
# wrong keep-choice is the hardest mistake to notice. Unknown groups sort after all of these.
RISK_RANK: dict[str, int] = {
    "temp_and_browser_caches": 0,
    "crash_dumps": 1,
    "package_caches": 2,
    "dev_artifacts": 3,
    "large_logs": 4,
    "old_installers": 5,
    "archive_pairs": 6,
    "duplicates": 7,
}
_UNKNOWN_RISK = 10
# Bits of the subset-sum search: sizes are rounded to target/_RESOLUTION. 4096 units keep the
# rounding error under 0.03% of the target, far inside the 5% accuracy band the spec asks for.
_RESOLUTION = 4096


class PlanCandidate(Protocol):
    """What the planner reads from a candidate (`reclaim.models.Candidate` satisfies it)."""

    @property
    def category(self) -> str: ...
    @property
    def category_group(self) -> str: ...
    @property
    def size_bytes(self) -> int: ...


@dataclass(frozen=True, slots=True)
class Constraints:
    max_tier: Literal["A", "both"] = "A"
    include_categories: tuple[str, ...] = ()
    exclude_categories: tuple[str, ...] = ()
    exclude_path_globs: tuple[str, ...] = ()
    max_items: int | None = None
    prefer: Prefer = "lowest_risk"


@dataclass(frozen=True, slots=True)
class Selection:
    rule_id_or_category: str
    category_group: str
    tier: str
    item_count: int
    bytes: int
    risk: int
    paths: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class FreeSpacePlan:
    target_bytes: int
    selections: tuple[Selection, ...]
    planned_bytes: int
    reaches_target: bool
    shortfall_bytes: int
    excluded: tuple[str, ...]  # why selections were left out: one line each


def _selections(
    candidates: Iterable[tuple[PlanCandidate, str, str]], constraints: Constraints
) -> tuple[list[Selection], list[str]]:
    """Groups `(candidate, tier, path)` into whole selections and applies the narrowing
    constraints. A selection with any member matching an excluded path glob is dropped whole:
    `delete` cannot execute part of a rule."""
    groups: dict[tuple[str, str], list[tuple[PlanCandidate, str]]] = {}
    for candidate, tier, path in candidates:
        groups.setdefault((candidate.category, tier), []).append((candidate, path))
    out: list[Selection] = []
    left_out: list[str] = []
    include = set(constraints.include_categories)
    exclude = set(constraints.exclude_categories)
    for (rule, tier), members in sorted(groups.items()):
        group = members[0][0].category_group
        name = f"{rule} (tier {tier})"
        if tier == "B" and constraints.max_tier != "both":
            left_out.append(f"{name}: tier B needs max_tier='both'")
            continue
        if include and rule not in include and group not in include:
            left_out.append(f"{name}: not in include_categories")
            continue
        if rule in exclude or group in exclude:
            left_out.append(f"{name}: in exclude_categories")
            continue
        paths = tuple(sorted(path for _c, path in members))
        hit = next((g for g in constraints.exclude_path_globs for p in paths if _glob(p, g)), None)
        if hit is not None:
            left_out.append(f"{name}: a member matches exclude_path_globs {hit!r}")
            continue
        out.append(
            Selection(
                rule_id_or_category=rule,
                category_group=group,
                tier=tier,
                item_count=len(members),
                bytes=sum(c.size_bytes for c, _p in members),
                risk=RISK_RANK.get(group, _UNKNOWN_RISK) + (100 if tier == "B" else 0),
                paths=paths,
            )
        )
    return out, left_out


def _glob(path: str, pattern: str) -> bool:
    norm = path.replace("\\", "/").casefold()
    return fnmatch.fnmatchcase(norm, pattern.replace("\\", "/").casefold())


def _lowest_risk_cover(
    ordered: Sequence[Selection], target: int, max_items: int | None
) -> list[Selection] | None:
    """The plan `lowest_risk` promises, exactly: the smallest risk class L such that some subset
    of the selections at risk <= L reaches `target` within `max_items`, and among those subsets
    the one with the smallest total (ties: fewer items). None if no class can.

    One pass of a 0/1 knapsack over sizes rounded up to target/_RESOLUTION, minimising the item
    count per reachable total, checked after each risk class. A selection at least as large as
    the target is only ever worth taking alone, so it is a candidate of its own instead of a
    knapsack item; that bounds the table at twice the target."""
    unit = max(1, target // _RESOLUTION)
    goal = -(-target // unit)
    cap = 2 * goal
    inf = 1 << 62
    limit = inf if max_items is None else max_items
    best_items = [inf] * (cap + 1)
    best_items[0] = 0
    taken: list[tuple[Selection, int, bytearray]] = []
    alone: list[Selection] = []
    i = 0
    while i < len(ordered):
        risk = ordered[i].risk
        while i < len(ordered) and ordered[i].risk == risk:
            s = ordered[i]
            w = max(1, -(-s.bytes // unit))  # ceil: never under-count a selection
            if w >= goal:
                alone.append(s)
            else:
                take = bytearray(cap + 1)
                for x in range(cap, w - 1, -1):
                    v = best_items[x - w] + s.item_count
                    if v < best_items[x]:
                        best_items[x] = v
                        take[x] = 1
                taken.append((s, w, take))
            i += 1
        options: list[tuple[int, int, list[Selection]]] = []
        for x in range(goal, cap + 1):
            if best_items[x] <= limit:
                sel = _rebuild(taken, x)
                if sum(s.bytes for s in sel) >= target:  # rounding up can leave it just short
                    options.append((x, best_items[x], sel))
                    break
        options.extend(
            (max(1, -(-s.bytes // unit)), s.item_count, [s]) for s in alone if s.item_count <= limit
        )
        if options:
            exact = [(sum(s.bytes for s in sel), items, sel) for _w, items, sel in options]
            exact = [o for o in exact if o[0] >= target]
            if exact:
                return min(exact, key=lambda o: (o[0], o[1]))[2]
    return None


def _rebuild(taken: Sequence[tuple[Selection, int, bytearray]], x: int) -> list[Selection]:
    chosen = []
    for s, w, take in reversed(taken):
        if take[x]:
            chosen.append(s)
            x -= w
    chosen.reverse()
    return chosen


def _order(selections: Sequence[Selection], prefer: Prefer) -> list[Selection]:
    if prefer == "largest_first":
        return sorted(selections, key=lambda s: (-s.bytes, s.risk, s.rule_id_or_category))
    if prefer == "fewest_items":
        return sorted(
            selections,
            key=lambda s: (-s.bytes / max(1, s.item_count), s.risk, s.rule_id_or_category),
        )
    return sorted(selections, key=lambda s: (s.risk, -s.bytes, s.rule_id_or_category))


def _within_items(chosen: list[Selection], max_items: int | None) -> bool:
    return max_items is None or sum(s.item_count for s in chosen) <= max_items


def plan_free_space(
    candidates: Iterable[tuple[PlanCandidate, str, str]],
    target_bytes: int,
    constraints: Constraints | None = None,
) -> FreeSpacePlan:
    """`candidates` are `(candidate, tier "A"/"B", path)` triples from the validator-filtered
    candidate list.

    `lowest_risk` (the default) uses the least risky selections that can reach the target: it
    grows the pool one risk class at a time and, within the first pool that can, picks the
    subset closest above the target. `largest_first` and `fewest_items` take selections in that
    order until the target is reached, then drop any that turned out not to be needed.
    `max_items` is a hard cap: a plan that would exceed it is cut back and reported short."""
    c = constraints or Constraints()
    if target_bytes <= 0:
        raise ValueError("target_bytes must be positive")
    selections, left_out = _selections(candidates, c)
    ordered = _order(selections, c.prefer)
    chosen: list[Selection] = []
    if c.prefer == "lowest_risk":
        cover = _lowest_risk_cover(ordered, target_bytes, c.max_items)
        chosen = cover if cover is not None else list(ordered)
    else:
        running = 0
        for s in ordered:
            if running >= target_bytes:
                break
            chosen.append(s)
            running += s.bytes
        for s in sorted(chosen, key=lambda s: -s.risk):  # drop what turned out not needed
            if sum(x.bytes for x in chosen) - s.bytes >= target_bytes:
                chosen.remove(s)
    while chosen and not _within_items(chosen, c.max_items):
        # The cap wins over the target (spec I6: never relax a constraint): drop the riskiest.
        worst = max(chosen, key=lambda s: (s.risk, s.item_count))
        chosen.remove(worst)
        left_out.append(f"{worst.rule_id_or_category} (tier {worst.tier}): over max_items")
    planned = sum(s.bytes for s in chosen)
    return FreeSpacePlan(
        target_bytes=target_bytes,
        selections=tuple(_order(chosen, c.prefer)),
        planned_bytes=planned,
        reaches_target=planned >= target_bytes,
        shortfall_bytes=max(0, target_bytes - planned),
        excluded=tuple(left_out),
    )
