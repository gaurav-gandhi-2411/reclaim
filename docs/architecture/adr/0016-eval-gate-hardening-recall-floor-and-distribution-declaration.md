# 0016. Eval gate hardening: recall/usefulness floor + required distribution declaration

## Context

ADR-0012's first real-data operating point (`max_hamming_distance = 14`) passed its gate —
precision 0.9600, clearing spec §7.3's ≥0.95 target — while its recall was 0.0764. That gate
had exactly one criterion: precision. Nothing in the code or the ADR process forced anyone to
ask "clears precision at what cost to usefulness?" or "measured against what, exactly?" until
GG read the number by hand and recognized both problems: the measurement was real, but it was
adversarial-tail-only (Copydays' `strong` split — print-and-scan/blur/paint, a deliberately
hard attack tier, not Feature 1a's actual target of ordinary consumer duplicates), and the gate
had no mechanism that would have caught that on its own.

This is a structural gap, not a one-off mistake to patch and move past. Any future feature
(starting immediately with build-order item 1b) built the same way — a real PR curve, a
precision-only target — could reproduce the exact same failure mode: technically measured,
technically passing, practically useless, on a distribution nobody had to name.

## Decision

**Every operating-point selection now requires two floors, not one.** `eval_harness.
select_operating_point` gained a required `min_recall` parameter. It first filters to points
clearing `target_precision` (unchanged), then — among those — requires the highest-recall
point to ALSO clear `min_recall`, returning `None` if it doesn't. A feature that's precise but
useless now fails its own gate mechanically, the same way a feature with no precision-qualifying
point at all already failed it.

`min_recall`'s value is a per-feature, per-track POLICY choice (not derived from data — there's
no dataset that tells you "0.5 is the right floor," the same way `target_precision = 0.95` isn't
derived from data either), and must be stated and justified at the call site, same as
`target_precision`. This ADR sets the default for the near-identical/deletion-suggestion track
GG's spec already scoped: **`0.5`** — a feature must catch at least half of the duplicates in
its own realistic-distribution measurement, or it isn't worth recommending even at perfect
precision. Future tracks may justify a different floor in their own ADR; `0.5` is a reasonable
starting default, not a universal constant.

**Every `select_operating_point` call now requires a `DistributionDeclaration`, not an
optional docstring mention.** A new frozen dataclass, structurally required (not defaulted):

```python
@dataclass(frozen=True, slots=True)
class DistributionDeclaration:
    description: str
    is_realistic: bool
    is_adversarial_tail_only: bool
    is_synthetic_only: bool
    untested_variation_note: str
```

`__post_init__` rejects an empty `description`, an empty `untested_variation_note` (even a
realistic measurement has a boundary — ADR-0012's realistic-distribution follow-up covers 5
transform profiles, not infinite real-world variation, and that boundary must be stated, not
implied to be exhaustive), and a declaration that claims to be simultaneously `is_realistic`
and `is_adversarial_tail_only`/`is_synthetic_only` (pick the honest one). This is enforced at
construction time — a caller cannot pass `DistributionDeclaration(description="", ...)` and
have it silently accepted the way an undocumented assumption previously could.

**`assert_safe_to_promote_to_measured(distribution)` is the structural gate on the word
"MEASURED" itself.** Raises `UnsafeMeasuredPromotionError` if `is_adversarial_tail_only` or
`is_synthetic_only` is `True` — exactly the two shapes of measurement that legitimately produce
real, honestly-reportable numbers (an adversarial tier's true recall; a synthetic fixture's
clean separation) but must never ALONE justify presenting an operating point as production-
basis MEASURED. A test asserting this function does not raise for a feature's actual chosen
operating point's distribution is now the structural proof a gate-hardening reviewer (or a
future verifier pass) can check mechanically, instead of re-reading ADR prose and trusting it
wasn't quietly wrong.

**The historical mistake is now a permanent regression test, not just a fixed number.**
`evals/test_ai_copydays_gold.py::test_real_pr_curve_and_operating_point_on_copydays` — the exact
measurement that produced the original 0.9600/0.0764 result — now asserts `select_operating_point`
returns `None` for that same curve under the hardened gate, with an explicit sanity check that
the OLD precision-only logic would have found a (useless) point. If this test ever starts
failing because `operating_point` is no longer `None`, that means either the recall-floor logic
regressed, or Copydays' adversarial tier's actual recall genuinely improved past the floor —
either way, a signal worth investigating, not a red herring to silence.

## Consequences

- `select_operating_point`'s signature changed (added required `min_recall`, `distribution`
  keyword arguments) — every existing call site was updated in the same change (ADR-0012's
  synthetic-fixture test, hard-tier test, and realistic-distribution test). This is a breaking
  API change made deliberately, not accidentally — the whole point is that the old
  precision-only call shape must no longer compile/pass without a caller consciously supplying
  both a recall floor and an honest distribution label.
- `OperatingPoint` gained a `distribution: DistributionDeclaration` field — any code
  destructuring or constructing an `OperatingPoint` directly (rather than via
  `select_operating_point`) needs updating too; none currently exists outside `eval_harness.py`
  and its tests.
- This does not retroactively invalidate ADR-0012's honestly-reported hard-tier numbers
  (precision 0.9600, recall 0.0764) — those remain true, disclosed measurements of that specific
  tier. What changes is that they can no longer be the sole basis for `max_hamming_distance =
  14`'s MEASURED status; ADR-0012's realistic-distribution follow-up is what actually satisfies
  the hardened gate now (see ADR-0012's retroactive disclosure section, added alongside this
  ADR).
- Every future feature ADR claiming a MEASURED operating point must show: (1) the precision
  floor cleared, (2) the recall/usefulness floor cleared, (3) a `DistributionDeclaration` with
  `is_realistic = True` (or an explicit justification for why a non-realistic distribution is
  being cited anyway, which `assert_safe_to_promote_to_measured` will refuse to let pass
  silently), and (4) the `untested_variation_note` — what the measurement does NOT cover. This
  is now the standard shape for every per-feature CI eval gate, starting with build-order item
  1b.

## Alternatives considered

- **Add a linter/doc-review checklist instead of code enforcement.** Rejected — this codebase's
  own established pattern (the AST import scan, `pydantic.extra="forbid"`, hardcoded
  `is_provisional=True`) is structural enforcement over documentation-only convention, precisely
  because documentation-only conventions are exactly what failed here: ADR-0012 already *could*
  have mentioned the distribution more prominently, and didn't, until GG caught it by hand.
- **Make `min_recall` a single hardcoded global constant instead of a per-call parameter.**
  Rejected — different tracks/features legitimately need different recall floors (the spec
  itself scopes different precision targets per track); hardcoding one number would either be
  too strict for some features or too loose for others, and forcing every call site to state
  its own floor keeps the choice visible and reviewable rather than buried in a shared constant.

## Test coverage

`tests/test_ai_eval_harness.py`: `DistributionDeclaration` validation (4 cases: empty
description, empty untested-variation-note, realistic+adversarial conflict, realistic+synthetic
conflict), `assert_safe_to_promote_to_measured` (3 cases: passes for realistic, rejects
adversarial-tail-only, rejects synthetic-only), `select_operating_point`'s new recall-floor
rejection (1 case, plus the two existing selection-machinery cases updated to the new required
arguments). `evals/test_ai_copydays_gold.py`'s hard-tier test is the live regression proof
against the actual historical incident. `evals/test_ai_copydays_realistic_distribution.py`'s
test calls `assert_safe_to_promote_to_measured` on the distribution ADR-0012 actually cites,
proving that citation is safe under the hardened gate, not just asserted to be.
