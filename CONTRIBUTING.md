# Contributing

This is a solo portfolio project, but issues and PRs are welcome.

## Dev setup

```powershell
uv sync --all-groups
uv run pytest              # unit tests
uv run pytest evals/ -v    # safety-gate + perf smoke tests (slower — real git ops)
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Frontend regression tests (jsdom, no browser download required):

```powershell
cd tests/frontend
npm ci
npm test
```

## Non-negotiables for a PR

- **`evals/test_safe_mode_gate.py` and `evals/test_ai_safety_gate.py` must pass.** These are the
  structural proof that safe mode's guarantees hold (ADR-0023) and that the AI layer stays
  recommend-only, never delete-capable (§7.5). A change that breaks either of these is a safety
  regression, not a style nit — it blocks the PR regardless of what else it does.
- **No fabricated metrics.** Every number in a commit message, README, or ADR must trace back to
  something actually measured or a test that actually ran — never an estimate presented as a
  measurement.
- **No confidence percentages in UI copy.** Reclaim's design commitment is deterministic rules
  with a stated mechanism ("rebuilds on next `npm install`"), not a probability score — this
  applies to both the rules engine and the recommend-only AI layer. Don't add fake-precision
  language like "92% confident" anywhere a user reads it.
- **Every bug fix ships with the test that would have caught it.** No exceptions — see house
  rule 80 in this author's engineering conventions.
- **Architecture decisions get an ADR.** New `docs/architecture/adr/NNNN-kebab-case-title.md`
  (Context / Decision / Consequences / Alternatives) for anything that changes a structural
  guarantee (safety boundaries, the mode model, the vault/manifest format) — not for routine bug
  fixes or refactors.

## Code style

- Python 3.12, type hints on all function signatures, `ruff check`/`ruff format` clean, `mypy`
  clean.
- Match the existing code's comment style: comments explain *why*, not *what* — especially
  around anything safety-relevant.
