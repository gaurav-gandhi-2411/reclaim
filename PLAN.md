# Reclaim — Execution Plan

**Tier:** T1 (portfolio project). Spec: `reclaim-spec.md` (authoritative — kept under that
filename rather than `spec.md` since it's referenced by that name throughout the build brief).

**Orchestration:** Opus/Fable orchestrator -> Sonnet executor -> Haiku verifier per
CLAUDE.md rules 69/70. Verifier signs off each stage against its CI gate before the next
stage starts. No auto-merge; GG merges.

**Non-negotiables (from spec, restated so they can't get lost mid-build):**
- Rules-first, no ML in Phase 1.
- SafetyValidator runs BEFORE candidate generation, every stage.
- Dry-run is the default; `--apply` is explicit and opt-in.
- `send2trash` only — no permanent deletes anywhere in v1.
- No fabricated confidence scores in code or UI copy (hash match = exact, pHash = a
  reported Hamming distance, heuristics labeled "heuristic").
- Never scan or modify GG's real disk during development — fixtures only. First real-disk
  run is dry-run mode, output a report for GG's review before anything is queued for apply.

## Stage order (riskiest assumption first)

| # | Stage | Status | CI gate |
|---|-------|--------|---------|
| 1 | SafetyValidator + golden fixture tree + hard CI gate | done | zero protected files ever appear in Tier A; build fails on any hit |
| 2 | Scanner (os.scandir, SQLite index, cloud-placeholder detection) | not started | placeholder-exclusion unit test passes; perf budget ≥100K files/min on SSD |
| 3 | Rule detectors (dev artifacts, caches, temp, dumps, installers, archive pairs, logs) | not started | detector fixtures pass, manifest-adjacency check enforced |
| 4 | Exact-duplicate pipeline (size bucket -> 64KB partial hash -> BLAKE3) | not started | precision = 1.0 on fixtures |
| 5 | Executor + quarantine manifest + batch undo | not started | every quarantined fixture file restorable in tests |
| 6 | FastAPI + dashboard (treemap, category cards, review queue, restore view) + visual identity | not started | dashboard renders against fixture data; no prior-project branding reused |

## Checkpoints

### 2026-07-12 — Repo scaffold
- Created `C:\Users\dev\ml-projects\reclaim`, git init, `uv init --package` pinned to
  Python 3.12.12.
- Structure: `src/reclaim/` (package), `tests/`, `evals/fixtures/`, `docs/architecture/adr/`,
  `data/` (gitignored — runtime scan index + quarantine, never committed), `scripts/`,
  `.github/workflows/`.
- `pyproject.toml`: ruff (E,F,I,UP,B,SIM,S,T20,DTZ,PTH,TRY,RUF; line-length 100), mypy
  strict on `src/reclaim`, pytest + pytest-asyncio (auto mode) + pytest-cov (80% floor on
  `src/reclaim`, `cli.py` omitted), deps: fastapi, uvicorn, pydantic v2, pydantic-settings,
  structlog, send2trash, blake3, jinja2.
- Added PM header (target user / pain point / success metric / who pays) to
  `reclaim-spec.md` per rule 20a.
- Next: Stage 1 (SafetyValidator + fixtures + CI gate) via executor subagent.

### 2026-07-12 — Stage 1 complete: SafetyValidator + golden fixture tree + hard CI gate
- `src/reclaim/models.py`: `FileRecord` (frozen dataclass, slots — zero-validation-overhead
  for the future ≥100K files/min scanner hot path), `Verdict` (BLOCKED/REVIEW_ONLY/ELIGIBLE),
  `SafetyResult`.
- `src/reclaim/config.py`: pydantic-settings `Config`/`SafetyConfig`/`CategoriesConfig`,
  `tomllib`-based loader, `config.example.toml` committed, real `config.toml` gitignored.
- `src/reclaim/safety.py`: `SafetyValidator` deny-first precedence — built-in deny (protected
  roots, in-git-repo except clean-repo+category-enabled node_modules, protected extensions/
  `.ssh`, DB/VM extensions, Docker/WSL roots, cloud placeholders) > user deny-list (always
  wins) > built-in review-only (finance/tax/legal) > user allow-list (promotes review-only
  only, never overrides deny) > default eligible.
- `evals/fixtures/golden_tree.json` (44 cases) + `build_golden_tree.py` (materializes a real
  temp dir tree, runs actual `git init`/commit/dirty) + `evals/test_safety_gate.py`: per-case
  verdict match, the hard gate (zero protected-category cases ever `ELIGIBLE`, fails loudly
  listing every leak), and a gate-coverage self-check so the hardcoded protected-category set
  can't silently drift from the manifest. `.github/workflows/eval.yml` added (separate from
  `ci.yml`, same cancel-in-progress pattern).
- `tests/test_safety.py`: 26 fast precedence unit tests.
- Verification (commands run by an independent Haiku verifier, not just the executor):
  `uv run ruff check .` — pass · `uv run ruff format --check .` — pass (9 files) ·
  `uv run mypy` — pass (6 source files) · `uv run pytest tests/ -v` — 26 passed ·
  `uv run pytest evals/ -v` — 3 passed. Verifier independently re-derived the precedence
  order from spec and confirmed it matches; confirmed golden tree has real negative
  (benign-eligible) cases, not just positives; hand-checked 4 fixture cases against spec
  wording; confirmed the hard-gate assertion fails loudly with full violation details, not a
  soft warning.
- Judgment calls made by executor (accepted): `FileRecord`/`SafetyResult` as frozen
  dataclasses not pydantic (hot-path perf); protected-root/Docker-WSL patterns as globs
  matched against posix-form paths so only the 4 OS-absolute roots need test overrides;
  cloud-placeholder bit stored in the fixture manifest rather than actually set on disk
  (not settable without a real cloud filter driver — `SafetyValidator` never touches disk
  itself, so this doesn't weaken the gate); `expected_reason_contains` checked against the
  human-readable rationale (the UI-facing string), not the reason code.
- Next: Stage 2 — Scanner (os.scandir multi-threaded walk, SQLite index, mtime incremental
  rescan, reparse-point safety, hardlink dedup, cloud-placeholder detection via
  `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` with a unit test proving placeholders are excluded).
  Scanner must populate `FileRecord.git_repo_root`/`git_repo_clean`/`attributes` for real and
  run every record through `SafetyValidator.filter_candidates()` before anything reaches the
  candidate pipeline.

## Gotchas discovered
- `uv init --package` created a `reclaim = "reclaim:main"` script entry pointing at a stub
  `main()`; repointed to `reclaim.cli:main` (placeholder) since Stage 2+ will define the real
  CLI surface.
- Default git branch from this git version is `master`; renamed to `main` before first
  commit per house rule 35 (only needed once, first commit hadn't landed yet).
