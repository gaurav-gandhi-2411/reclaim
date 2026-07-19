# 0018. Metrics-integrity invariant: never aggregate precision/recall across distinct tiers

## Context

ADR-0017's templated-document follow-up measured Feature 1b's operating point across two
distinct distribution tiers: a large "prose" tier (7,140 negatives, cleanly separated) and a
small "templated" tier (459 negatives, a real precision failure). The first version of that
measurement pooled both tiers' pairs into one combined precision calculation. At the grid
point `(minhash=0.1, embedding=0.875)`, the pooled calculation read **500 true positives / 25
false positives = 0.9524 precision** — clearing the 0.95 target. The templated tier **alone**,
at that same point, was actually **158 true positives / 25 false positives = 0.8634
precision** — a real, serious failure the pooled number completely hid, because the large
prose tier's 7,140 cleanly-rejected negatives mathematically diluted the small tier's 25 real
false positives into statistical insignificance.

This is the same *shape* of mistake as ADR-0012's original recall-artifact incident (a real
number that looked fine while hiding a real problem) — but a different, distinct mechanism.
ADR-0012/ADR-0016 addressed "measuring only one (unrepresentative) distribution and calling it
representative." This is "measuring several distributions correctly, then destroying the
signal by pooling them before computing the metric." Both are real, both recur unless the
harness itself forbids them structurally — this ADR is that structural forbidding, for the
second mechanism, generalized beyond Feature 1b.

GG's framing is the operative one: **this is a metrics-integrity invariant, not a 1b-specific
fix.** Any future feature that measures against more than one named tier (any decomposition —
by content type, by attack severity, by document format, by whatever a future feature's
distribution naturally splits into) must never pool those tiers into one number.

## Decision

**`eval_harness.py` gained `select_operating_point_per_tier` (1D) and
`select_joint_operating_point_per_tier` (2D)** — the tier-gated analogs of the existing
`select_operating_point`/`select_joint_operating_point`. Both take a `Mapping[str, ...]` from
tier name to that tier's own positive/negative data, and both compute precision and recall
**exclusively from within each tier's own data** — there is no code path in either function
that concatenates, sums, or otherwise pools counts across tiers before computing a ratio. A
candidate threshold only qualifies if **every** declared tier independently clears both the
precision target and the recall floor; among qualifying thresholds, the one maximizing the
**minimum** recall across tiers wins (not the mean — a threshold that's excellent for one tier
and merely adequate for another must not be preferred over one that's good for both, since the
weaker tier is the one actually at risk of the ADR-0012/ADR-0017 failure mode recurring).

**The existing single-distribution functions (`select_operating_point`,
`select_joint_operating_point`) are unchanged and remain correct** — they were never wrong for
a single declared distribution; the mistake was only ever pooling *multiple* distributions
through them. They stay in `eval_harness.py` as the right tool for the single-tier case (e.g.,
ADR-0012's Copydays hard-tier measurement, which is genuinely one distribution). The moment a
measurement has more than one named tier, the `_per_tier` variants are the only correct choice.

**The real incident is now a permanent regression test**, reproducing the exact numbers:
`tests/test_ai_eval_harness.py::
test_select_joint_operating_point_per_tier_rejects_the_real_adr0017_incident` reconstructs the
342/18/0/7140 (prose) and 158/4/25/434 (templated) counts that produced the real 0.9524-pooled
/ 0.8634-templated-alone split, first proves the naive pooled function
(`select_joint_operating_point`, called with both tiers' pairs concatenated) **would** accept
this operating point — reproducing the bug as a documented fact, not a strawman — then proves
`select_joint_operating_point_per_tier` **rejects** it. If this test ever starts passing with
`gated_result is not None`, the per-tier gating logic has regressed and the incident could
recur silently.

**`evals/test_ai_document_templated_gold.py` was refactored to call the new harness function**
instead of its original hand-rolled grid search — the fix lives in one tested, reusable place,
not copy-pasted into every future multi-tier eval. Same measured operating point
(`minhash=0.1, embedding=0.95`) reproduces exactly through the harness call as it did through
the original inline loop, confirming the refactor is behavior-preserving.

**A real edge case caught while writing the regression test, not after**: both new functions
crashed with a bare `min() iterable argument is empty` if called with an empty `tiers` mapping
(the `min(...)` over `per_tier.values()` in the "is this better than `best`" comparison has
nothing to compare against when the tiers mapping itself is empty). Fixed with an explicit
`ValueError` guard at the top of both functions, and a dedicated regression test for it.

## Consequences

- Every future eval that measures across more than one named tier must use
  `select_operating_point_per_tier`/`select_joint_operating_point_per_tier`, never construct
  its own pooled calculation. A code reviewer (human or verifier) seeing a hand-rolled
  multi-tier precision/recall loop in a future eval file should treat it as a red flag —
  exactly the shape of bug this ADR exists to prevent recurring — and ask for it to call the
  shared, tested harness function instead.
- The "maximize minimum recall across tiers" selection rule is itself a policy choice, not
  derived from data (same status as `target_precision = 0.95` or the `0.5` recall floor
  default) — a future feature with a good reason to weight tiers differently (e.g., one tier
  representing 95% of real-world volume vs. a rare edge case) may justify a different
  combination rule in its own ADR; this default optimizes for "don't let any declared tier be
  the weak link," which is the property that actually would have caught ADR-0017's incident.
- This does not retroactively invalidate any SINGLE-tier measurement already in this repo
  (ADR-0012's Copydays hard-tier curve, ADR-0015's realistic-distribution curve, ADR-0017's
  original prose-only measurement) — those were never pooled across tiers to begin with.
- **Honestly disclosed residual risk**: `select_operating_point`/`select_joint_operating_point`
  (the original single-distribution functions) are still fully valid and still exist — nothing
  stops a future caller from concatenating multiple tiers' pairs and passing the pooled result
  to them anyway, reproducing the exact incident this ADR fixes. Neither function can detect
  pooled input from the data alone (there is no signal in a flat list of pairs that says "this
  came from more than one source"), so this cannot be closed with a runtime guard — both
  docstrings now carry an explicit "STOP: is this pooled?" warning naming this ADR and the
  `_per_tier` alternative, but the actual backstop remains code-review discipline, same as any
  API that can be misused by a caller ignoring its documented contract. Noted here rather than
  presented as fully solved.

## Test coverage

`tests/test_ai_eval_harness.py`: the exact-incident regression test (reproducing 0.9524
pooled / 0.8634 per-tier from real counts), a complementary positive case (every tier
genuinely qualifies, no aggregate anywhere on the result), a 1D single-stage analog of the
same invariant, and two empty-`tiers`-mapping edge-case tests (one per function) for the crash
caught while writing this suite. `evals/test_ai_document_templated_gold.py` is the live,
real-data proof that the refactored eval reproduces the exact same measured operating point
through the shared harness call.
