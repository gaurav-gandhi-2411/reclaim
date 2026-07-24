# Contributing

This is a solo portfolio project, but issues and PRs are welcome.

## Dev setup

```powershell
uv sync --all-groups
uv run python scripts/verify.py    # THE required check before any push -- see below
```

`scripts/verify.py` is the single canonical pre-push/pre-PR command: ruff check, ruff format
--check, mypy, `pytest tests/` plus the three safety-gate eval files (`evals/test_safety_gate.py`,
`evals/test_ai_safety_gate.py`, `evals/test_safe_mode_gate.py`), then the per-module coverage
floor. **Never substitute a hand-picked subset of this** — `pyproject.toml`'s
`testpaths = ["tests"]` means a bare `uv run pytest`/`uv run pytest tests/ -q` silently skips
every safety-gate eval, which is exactly how a real config-security regression once reached PR
review undetected (see docs/architecture/adr/0027-schema-versioning-for-durable-state.md's "A
real regression this ADR caused" section). Never report "the full test suite passes" from a
command that omits `evals/test_safety_gate.py`/`test_ai_safety_gate.py`/`test_safe_mode_gate.py`.

For the slower gold-set/operating-point evals (need the `[ai]` extra and real fixture datasets —
not part of the required pre-push check, run only when touching AI-layer detection quality):

```powershell
uv sync --all-groups --extra ai
uv run pytest evals/ -v
```

Frontend regression tests (jsdom, no browser download required):

```powershell
cd tests/frontend
npm ci
npm test
```

## Non-negotiables for a PR

- **`scripts/verify.py` must pass in full** — `evals/test_safety_gate.py`, `test_ai_safety_gate.py`,
  and `test_safe_mode_gate.py` are the structural proof that the deterministic deny-first gate
  (spec principle 3), the AI layer's recommend-only guarantee (§7.5), and safe mode's guarantees
  (ADR-0023) all still hold. A change that breaks any of the three is a safety regression, not a
  style nit — it blocks the PR regardless of what else it does, and regardless of whether
  `pytest tests/` alone stayed green (it would — that command doesn't run any of them).
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
